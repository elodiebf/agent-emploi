"""
Agent de recherche d'emploi — version économe en tokens.

Principe : tout ce qui peut être fait en Python est fait en Python
(recherche Adzuna, nettoyage, dédoublonnage, distance, écriture Notion).
Claude n'intervient que là où il est utile :
  1. Notation des offres  -> modèle léger (Haiku), par lots, une seule requête par lot.
  2. Contact + message    -> modèle intermédiaire (Sonnet) avec recherche web,
                             uniquement pour les offres retenues et nouvelles.

Règle anti-hallucination : un contact n'est enregistré que si sa source
est une page réellement renvoyée par la recherche web. Sinon : "Non trouvé".
"""

import os
import re
import json
import html
from datetime import date
from math import radians, sin, cos, sqrt, atan2

import anthropic
import requests
from dotenv import load_dotenv
from notion_client import Client

load_dotenv()
from profil import MON_PROFIL

# ─────────────────────────── CONFIGURATION ───────────────────────────

MODELE_TRI = "claude-haiku-5-5"        # notation des offres (peu coûteux)
MODELE_CONTACT = "claude-sonnet-5-5"   # recherche de contact + message

REQUETES_SALARIAT = ["Lead Data Engineer", "Chef de projet data", "Senior Data Analyst"]
REQUETES_FREELANCE = ["Data Engineer", "Data Analyst"]
RESULTATS_PAR_REQUETE = 20

SEUIL_SAUVEGARDE = 80           # score minimum pour enregistrer une offre
MAX_CONTACTS_PAR_JOUR = 15      # plafond de recherches de contact par exécution
RECHERCHES_WEB_PAR_OFFRE = 3    # plafond de recherches web par offre (0,01 $ l'une)
TAILLE_LOT_NOTATION = 25
LONGUEUR_DESCRIPTION = 600      # caractères de description envoyés pour la notation
LONGUEUR_MAX_MESSAGE = 300      # note d'invitation LinkedIn

# DRY_RUN=1 : n'écrit rien dans Notion, affiche ce qui serait enregistré.
DRY_RUN = os.getenv("DRY_RUN") == "1"

MOUGINS_LAT, MOUGINS_LON = 43.6000, 7.0333

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
notion = Client(auth=os.getenv("NOTION_API_KEY"))

DB_OFFRES = os.getenv("NOTION_DB_OFFRES")
DB_FREELANCE = os.getenv("NOTION_DB_FREELANCE")

# Suivi de la consommation, affiché en fin d'exécution
CONSO = {"input": 0, "output": 0, "cache_read": 0, "recherches_web": 0, "appels": 0}


def compter(response):
    u = response.usage
    CONSO["appels"] += 1
    CONSO["input"] += getattr(u, "input_tokens", 0) or 0
    CONSO["output"] += getattr(u, "output_tokens", 0) or 0
    CONSO["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
    stu = getattr(u, "server_tool_use", None)
    if stu is not None:
        CONSO["recherches_web"] += getattr(stu, "web_search_requests", 0) or 0


# ─────────────────────────── OUTILS PYTHON ───────────────────────────

def distance_km(lat, lon):
    R = 6371
    dlat = radians(lat - MOUGINS_LAT)
    dlon = radians(lon - MOUGINS_LON)
    a = sin(dlat / 2) ** 2 + cos(radians(MOUGINS_LAT)) * cos(radians(lat)) * sin(dlon / 2) ** 2
    return round(R * 2 * atan2(sqrt(a), sqrt(1 - a)))


def geocoder(ville):
    try:
        r = requests.get("https://api-adresse.data.gouv.fr/search/",
                         params={"q": ville, "limit": 1}, timeout=10)
        feats = r.json().get("features") or []
        if not feats:
            return None
        lon, lat = feats[0]["geometry"]["coordinates"]
        return lat, lon
    except Exception:
        return None


def formater_lieu(lieu, lat=None, lon=None, texte=""):
    if not lieu:
        return "Non précisé"
    if any(m in (lieu + " " + texte).lower()
           for m in ["remote", "télétravail", "teletravail", "distanciel"]):
        prefixe = "Remote possible 🌐 – "
    else:
        prefixe = ""
    if lat is None or lon is None:
        coords = geocoder(lieu)
        if coords:
            lat, lon = coords
    if lat is not None and lon is not None:
        return f"{prefixe}{lieu} ({distance_km(lat, lon)} km de Mougins)"
    return f"{prefixe}{lieu}"


def nettoyer(texte):
    texte = html.unescape(re.sub(r"<[^>]+>", " ", texte or ""))
    return re.sub(r"\s+", " ", texte).strip()


def cle_texte(titre, entreprise):
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return f"{norm(titre)}|{norm(entreprise)}"


def id_adzuna_depuis_url(url):
    m = re.search(r"/(?:ad|details)/(\d+)", url or "")
    return m.group(1) if m else None


def salaire(o):
    mn, mx = o.get("salary_min"), o.get("salary_max")
    if mn and mx and mn != mx:
        return f"{int(mn):,} – {int(mx):,} €".replace(",", " ")
    if mn or mx:
        return f"{int(mn or mx):,} €".replace(",", " ")
    return "N/A"


def chercher_adzuna(requete, freelance=False):
    params = {
        "app_id": os.getenv("ADZUNA_APP_ID"),
        "app_key": os.getenv("ADZUNA_APP_KEY"),
        "results_per_page": RESULTATS_PAR_REQUETE,
        "what": requete,
        "content-type": "application/json",
    }
    if freelance:
        params["contract"] = 1
    try:
        r = requests.get("https://api.adzuna.com/v1/api/jobs/fr/search/1",
                         params=params, timeout=20)
        r.raise_for_status()
        resultats = r.json().get("results", [])
    except Exception as e:
        print(f"⚠️  Adzuna en erreur pour « {requete} » : {e}")
        return []

    offres = []
    for o in resultats:
        offres.append({
            "id": str(o.get("id", "")),
            "titre": o.get("title", "").strip(),
            "entreprise": (o.get("company") or {}).get("display_name", "").strip(),
            "lieu_brut": (o.get("location") or {}).get("display_name", ""),
            "lat": o.get("latitude"),
            "lon": o.get("longitude"),
            "description": nettoyer(o.get("description", "")),
            "salaire": salaire(o),
            "lien": o.get("redirect_url", ""),
        })
    return offres


# ─────────────────────────── NOTION ───────────────────────────

def lire_base(database_id):
    """Lit toutes les lignes d'une base via l'API REST (version fixée)."""
    lignes, curseur = [], None
    while True:
        body = {"page_size": 100}
        if curseur:
            body["start_cursor"] = curseur
        r = requests.post(
            f"https://api.notion.com/v1/databases/{database_id}/query",
            headers={"Authorization": f"Bearer {os.getenv('NOTION_API_KEY')}",
                     "Notion-Version": "2022-06-28",
                     "Content-Type": "application/json"},
            json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        lignes += data.get("results", [])
        if not data.get("has_more"):
            return lignes
        curseur = data.get("next_cursor")


def texte_prop(props, nom):
    p = props.get(nom) or {}
    blocs = p.get("title") or p.get("rich_text") or []
    return "".join(b.get("plain_text", "") for b in blocs)


def charger_deja_vus(database_id, titre_prop):
    """Renvoie (ids Adzuna, clés titre|entreprise, contacts vérifiés par entreprise)."""
    ids, cles, contacts = set(), set(), {}
    if not database_id:
        return ids, cles, contacts
    try:
        lignes = lire_base(database_id)
    except Exception as e:
        print(f"⚠️  Impossible de lire la base Notion pour le dédoublonnage : {e}")
        return ids, cles, contacts
    for ligne in lignes:
        props = ligne.get("properties", {})
        url = (props.get("Lien") or {}).get("url")
        if id_adzuna_depuis_url(url):
            ids.add(id_adzuna_depuis_url(url))
        entreprise = texte_prop(props, "Entreprise") or texte_prop(props, "Client")
        cles.add(cle_texte(texte_prop(props, titre_prop), entreprise))
        fiab = ((props.get("Contact – Fiabilité") or {}).get("select") or {}).get("name")
        if fiab == "Vérifié" and entreprise:
            contacts[entreprise.lower()] = {
                "nom": texte_prop(props, "Contact – Nom"),
                "poste": texte_prop(props, "Contact – Poste"),
                "linkedin": (props.get("Contact – LinkedIn") or {}).get("url"),
                "source": (props.get("Contact – Source") or {}).get("url"),
                "fiabilite": "Vérifié",
            }
    print(f"📚 {len(lignes)} lignes déjà en base, {len(contacts)} contacts vérifiés réutilisables")
    return ids, cles, contacts


def rt(texte):
    return {"rich_text": [{"text": {"content": (texte or "")[:2000]}}]} if texte else {"rich_text": []}


def enregistrer_offre(o, contact, message):
    props = {
        "Titre du poste": {"title": [{"text": {"content": o["titre"][:2000]}}]},
        "Entreprise": rt(o["entreprise"]),
        "Score": {"number": o["score"]},
        "Salaire": rt(o["salaire"]),
        "Mission": rt(o["description"]),
        "Lien": {"url": o["lien"] or None},
        "Lieu": rt(formater_lieu(o["lieu_brut"], o["lat"], o["lon"], o["description"])),
        "Date": {"date": {"start": str(date.today())}},
        "Contact – Nom": rt(contact.get("nom")),
        "Contact – Poste": rt(contact.get("poste")),
        "Contact – LinkedIn": {"url": contact.get("linkedin") or None},
        "Contact – Source": {"url": contact.get("source") or None},
        "Contact – Fiabilité": {"select": {"name": contact.get("fiabilite", "Non trouvé")}},
        "Message LinkedIn": rt(message),
    }
    if DRY_RUN:
        print(json.dumps({"offre": o["titre"], "entreprise": o["entreprise"], "score": o["score"],
                          "contact": contact, "message": message}, ensure_ascii=False, indent=2))
        return
    notion.pages.create(parent={"database_id": DB_OFFRES}, properties=props)


def enregistrer_mission(o):
    if not DB_FREELANCE:
        print("⚠️  NOTION_DB_FREELANCE non défini : mission non enregistrée")
        return
    props = {
        "Titre de la mission": {"title": [{"text": {"content": o["titre"][:2000]}}]},
        "Client": rt(o["entreprise"]),
        "Score": {"number": o["score"]},
        "TJM proposé": rt(o["salaire"]),
        "Durée": rt("N/A"),
        "Lieu": rt(formater_lieu(o["lieu_brut"], o["lat"], o["lon"], o["description"])),
        "Mission": rt(o["description"]),
        "Lien": {"url": o["lien"] or None},
        "Plateforme": rt("Adzuna"),
        "Date": {"date": {"start": str(date.today())}},
    }
    if DRY_RUN:
        print(f"[DRY_RUN] mission : {o['titre']} – {o['entreprise']} ({o['score']})")
        return
    notion.pages.create(parent={"database_id": DB_FREELANCE}, properties=props)


# ─────────────────────────── CLAUDE ───────────────────────────

def texte_reponse(response):
    return "".join(b.text for b in response.content if getattr(b, "type", "") == "text")


def extraire_json(texte, ouvrant="{", fermant="}"):
    debut, fin = texte.find(ouvrant), texte.rfind(fermant)
    if debut == -1 or fin == -1:
        return None
    try:
        return json.loads(texte[debut:fin + 1])
    except json.JSONDecodeError:
        return None


def noter_offres(offres, type_poste):
    """Note les offres par lots avec le modèle léger. Ajoute o['score']."""
    for i in range(0, len(offres), TAILLE_LOT_NOTATION):
        lot = offres[i:i + TAILLE_LOT_NOTATION]
        compact = [{"id": o["id"], "titre": o["titre"], "entreprise": o["entreprise"],
                    "lieu": o["lieu_brut"], "salaire": o["salaire"],
                    "description": o["description"][:LONGUEUR_DESCRIPTION]} for o in lot]
        response = claude.messages.create(
            model=MODELE_TRI,
            max_tokens=2000,
            system=(
                "Tu évalues l'adéquation entre des offres et un profil. "
                "Note chaque offre de 0 à 100 : intitulé et niveau de séniorité, compétences, "
                "localisation (proche de Mougins/Cannes–Monaco ou remote), salaire si indiqué. "
                "Ne suppose rien qui n'est pas écrit dans l'offre.\n\n"
                f"Profil :\n{MON_PROFIL}"
            ),
            messages=[{"role": "user", "content":
                f"Type : {type_poste}\nOffres :\n{json.dumps(compact, ensure_ascii=False)}\n\n"
                'Réponds uniquement par un tableau JSON : [{"id": "...", "score": 0}]'}],
        )
        compter(response)
        notes = extraire_json(texte_reponse(response), "[", "]") or []
        par_id = {str(n.get("id")): n.get("score") for n in notes if isinstance(n, dict)}
        for o in lot:
            s = par_id.get(o["id"])
            o["score"] = int(s) if isinstance(s, (int, float)) else 0
        manquants = sum(1 for o in lot if o["id"] not in par_id)
        if manquants:
            print(f"⚠️  {manquants} offre(s) non notée(s) dans ce lot (score 0)")


CONSIGNES_MESSAGE = f"""Message LinkedIn de première prise de contact :
- {LONGUEUR_MAX_MESSAGE - 20} caractères maximum, en français, vouvoiement, ton professionnel et direct ;
- commence par « Bonjour [Prénom], » si un contact est identifié, sinon « Bonjour, » ;
- mentionne l'intitulé exact de l'offre et 1 ou 2 compétences du profil en lien avec elle ;
- n'utilise QUE des faits présents dans le profil : aucune expérience, aucun chiffre, aucune
  compétence inventés ou exagérés ;
- pas de formule creuse, pas d'emoji ; signe « Elodie »."""


def chercher_contact_et_message(o):
    """Recherche web d'un contact pour l'offre + rédaction du message."""
    prompt = f"""Offre :
- Intitulé : {o['titre']}
- Entreprise : {o['entreprise'] or 'non précisée'}
- Lieu : {o['lieu_brut']}
- Extrait : {o['description'][:LONGUEUR_DESCRIPTION]}

Tâche 1 — Trouve UNE personne à contacter pour cette offre, par ordre de préférence :
recruteur/talent acquisition de l'entreprise, responsable data/BI, DRH.
Utilise la recherche web (au plus {RECHERCHES_WEB_PAR_OFFRE} recherches).
Règles strictes :
- N'indique un nom que s'il apparaît explicitement dans une page de résultats,
  et donne l'URL exacte de cette page dans "source".
- "linkedin" : uniquement une URL de profil linkedin.com/in/ apparue dans les résultats, sinon null.
- "Vérifié" = nom ET poste actuel dans l'entreprise confirmés par la source ;
  "À vérifier" = nom trouvé mais poste ou ancienneté incertains ;
  "Non trouvé" = aucune source fiable (nom, poste, linkedin et source à null).
- Ne devine jamais un nom. Une case vide vaut mieux qu'un nom inventé.

Tâche 2 — {CONSIGNES_MESSAGE}

Profil de la candidate :
{MON_PROFIL}

Termine ta réponse par ce JSON seul, sans texte après :
{{"nom": null, "poste": null, "linkedin": null, "source": null, "fiabilite": "Non trouvé", "message": "..."}}"""

    messages = [{"role": "user", "content": prompt}]
    outils = [{"type": "web_search_20250305", "name": "web_search",
               "max_uses": RECHERCHES_WEB_PAR_OFFRE,
               "user_location": {"type": "approximate", "country": "FR"}}]

    urls_vues, response = set(), None
    for _ in range(3):  # reprises en cas de pause_turn
        response = claude.messages.create(model=MODELE_CONTACT, max_tokens=1500,
                                          tools=outils, messages=messages)
        compter(response)
        for bloc in response.content:
            if getattr(bloc, "type", "") == "web_search_tool_result":
                contenu = getattr(bloc, "content", None)
                if isinstance(contenu, list):
                    for r in contenu:
                        if getattr(r, "url", None):
                            urls_vues.add(r.url.rstrip("/"))
        if response.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": response.content}]

    resultat = extraire_json(texte_reponse(response)) or {}
    contact = valider_contact(resultat, urls_vues)
    message = (resultat.get("message") or "").strip()
    if not contact["nom"]:
        # contact rejeté ou absent : on retire tout prénom de la salutation
        message = re.sub(r"^Bonjour\s+[^,\n]+,", "Bonjour,", message)
    return contact, message


def valider_contact(c, urls_vues):
    """Garde-fou : on ne conserve que ce qui est adossé à une page réellement consultée."""
    vide = {"nom": None, "poste": None, "linkedin": None, "source": None, "fiabilite": "Non trouvé"}
    source = (c.get("source") or "").rstrip("/")
    linkedin = (c.get("linkedin") or "").rstrip("/")
    if not c.get("nom") or not source or source not in urls_vues:
        if c.get("nom"):
            print(f"   ↳ contact écarté (source non issue de la recherche) : {c.get('nom')}")
        return vide
    if linkedin and (linkedin not in urls_vues or "linkedin.com/in/" not in linkedin):
        linkedin = None
    fiab = c.get("fiabilite") if c.get("fiabilite") in ("Vérifié", "À vérifier") else "À vérifier"
    return {"nom": c["nom"], "poste": c.get("poste"), "linkedin": linkedin or None,
            "source": source, "fiabilite": fiab}


def rediger_message_seul(o, contact):
    """Contact déjà connu : message rédigé par le modèle léger, sans recherche web."""
    response = claude.messages.create(
        model=MODELE_TRI, max_tokens=400,
        messages=[{"role": "user", "content":
            f"{CONSIGNES_MESSAGE}\n\nContact : {contact.get('nom')} ({contact.get('poste')})\n"
            f"Offre : {o['titre']} chez {o['entreprise']}\n\nProfil :\n{MON_PROFIL}\n\n"
            "Réponds uniquement par le message."}])
    compter(response)
    return texte_reponse(response).strip()


def raccourcir(message):
    if len(message) <= LONGUEUR_MAX_MESSAGE:
        return message
    response = claude.messages.create(
        model=MODELE_TRI, max_tokens=300,
        messages=[{"role": "user", "content":
            f"Raccourcis ce message à moins de {LONGUEUR_MAX_MESSAGE - 20} caractères, "
            f"sans ajouter aucune information. Réponds uniquement par le message.\n\n{message}"}])
    compter(response)
    court = texte_reponse(response).strip()
    if len(court) > LONGUEUR_MAX_MESSAGE:
        print(f"   ↳ ⚠️ message encore trop long ({len(court)} car.), à raccourcir à la main")
    return court


# ─────────────────────────── ORCHESTRATION ───────────────────────────

def nouvelles_offres(offres, ids_vus, cles_vues):
    uniques, vus = [], set()
    for o in offres:
        cle = cle_texte(o["titre"], o["entreprise"])
        if not o["id"] or o["id"] in ids_vus or cle in cles_vues or o["id"] in vus or cle in vus:
            continue
        vus.update({o["id"], cle})
        uniques.append(o)
    return uniques


def partie_salariat():
    ids_vus, cles_vues, contacts_connus = charger_deja_vus(DB_OFFRES, "Titre du poste")
    brutes = [o for q in REQUETES_SALARIAT for o in chercher_adzuna(q)]
    offres = nouvelles_offres(brutes, ids_vus, cles_vues)
    print(f"🔎 Salariat : {len(brutes)} résultats, {len(offres)} nouvelles offres à noter")
    if not offres:
        return
    noter_offres(offres, "CDI / salariat")
    retenues = sorted([o for o in offres if o["score"] >= SEUIL_SAUVEGARDE],
                      key=lambda o: o["score"], reverse=True)
    if len(retenues) > MAX_CONTACTS_PAR_JOUR:
        print(f"ℹ️  {len(retenues) - MAX_CONTACTS_PAR_JOUR} offre(s) reportée(s) à demain (plafond)")
        retenues = retenues[:MAX_CONTACTS_PAR_JOUR]

    cache = dict(contacts_connus)
    for o in retenues:
        print(f"👤 {o['score']:>3} | {o['titre']} – {o['entreprise']}")
        connu = cache.get((o["entreprise"] or "").lower())
        try:
            if connu:
                contact, message = connu, rediger_message_seul(o, connu)
            else:
                contact, message = chercher_contact_et_message(o)
                if contact["fiabilite"] == "Vérifié" and o["entreprise"]:
                    cache[o["entreprise"].lower()] = contact
            enregistrer_offre(o, contact, raccourcir(message))
        except Exception as e:
            print(f"   ↳ ❌ erreur, offre non enregistrée (réessayée au prochain passage) : {e}")


def partie_freelance():
    if not DB_FREELANCE:
        print("ℹ️  Partie freelance ignorée : NOTION_DB_FREELANCE non défini")
        return
    ids_vus, cles_vues, _ = charger_deja_vus(DB_FREELANCE, "Titre de la mission")
    brutes = [o for q in REQUETES_FREELANCE for o in chercher_adzuna(q, freelance=True)]
    missions = nouvelles_offres(brutes, ids_vus, cles_vues)
    print(f"🔎 Freelance : {len(brutes)} résultats, {len(missions)} nouvelles missions à noter")
    if not missions:
        return
    noter_offres(missions, "mission freelance / contrat")
    for o in missions:
        if o["score"] >= SEUIL_SAUVEGARDE:
            try:
                enregistrer_mission(o)
            except Exception as e:
                print(f"   ↳ ❌ mission non enregistrée : {e}")


def lancer_agent():
    print("🤖 Le robot démarre..." + (" (DRY_RUN : rien n'est écrit dans Notion)" if DRY_RUN else ""))
    partie_salariat()
    partie_freelance()
    print(f"\n📊 Consommation : {CONSO['appels']} appels, {CONSO['input']} tokens en entrée, "
          f"{CONSO['output']} en sortie, {CONSO['recherches_web']} recherches web")
    print("✅ Terminé ! Regarde ton Notion.")


if __name__ == "__main__":
    lancer_agent()
