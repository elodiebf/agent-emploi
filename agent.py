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
import time
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

# [CORRECTIF] ajout de « Analytics Engineer » (ton positionnement) — retire-le si tu ne le veux pas
REQUETES_SALARIAT = ["Lead Data Engineer", "Chef de projet data", "Senior Data Analyst",
                     "Analytics Engineer"]
RESULTATS_PAR_REQUETE = 20

# [CORRECTIF] fenêtre de fraîcheur courte pour ne pas renoter chaque jour les mêmes offres.
# Le lundi, fenêtre élargie pour couvrir le week-end (le robot ne tourne qu'en semaine).
LUNDI = date.today().weekday() == 0
ADZUNA_MAX_JOURS = 4 if LUNDI else 2      # paramètre Adzuna « max_days_old »

# France Travail (API Offres d'emploi v2) — activé si les identifiants sont présents
FT_CLIENT_ID = os.getenv("FT_CLIENT_ID")
FT_CLIENT_SECRET = os.getenv("FT_CLIENT_SECRET")
FT_DEPARTEMENTS = ["06"]        # Alpes-Maritimes (le remote national reste couvert par Adzuna)
FT_PUBLIEE_DEPUIS = 3 if LUNDI else 1   # valeurs acceptées par l'API : 1, 3, 7, 14, 31
FT_TYPE_CONTRAT = "CDI"         # [CORRECTIF] exclut CDD, intérim, saisonnier
FT_RESULTATS_PAR_REQUETE = 50

SEUIL_SAUVEGARDE = 60           # score minimum pour enregistrer une offre
MAX_CONTACTS_PAR_JOUR = 15      # plafond de recherches de contact par exécution
RECHERCHES_WEB_PAR_OFFRE = 3    # plafond de recherches web par offre (0,01 $ l'une)
TAILLE_LOT_NOTATION = 25
LONGUEUR_DESCRIPTION = 1200      # caractères de description envoyés pour la notation
LONGUEUR_MAX_MESSAGE = 300      # note d'invitation LinkedIn

# DRY_RUN=1 : n'écrit rien dans Notion, affiche ce qui serait enregistré.
DRY_RUN = os.getenv("DRY_RUN") == "1"

MOUGINS_LAT, MOUGINS_LON = 43.6000, 7.0333

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
notion = Client(auth=os.getenv("NOTION_API_KEY"))

DB_OFFRES = os.getenv("NOTION_DB_OFFRES")
# [AJOUT] base des offres notées sous le seuil : sert uniquement à ne pas les renoter
DB_ECARTEES = os.getenv("NOTION_DB_ECARTEES")
PAUSE_NOTION = 0.35             # secondes entre deux écritures (limite Notion ~3 requêtes/s)

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
    """Identifiant stable d'une offre à partir de son lien (Adzuna ou France Travail)."""
    m = re.search(r"francetravail\.fr/offres/recherche/detail/(\w+)", url or "")
    if m:
        return f"ft-{m.group(1)}"
    m = re.search(r"/(?:ad|details)/(\d+)", url or "")
    return m.group(1) if m else None


def salaire(o):
    mn, mx = o.get("salary_min"), o.get("salary_max")
    if mn and mx and mn != mx:
        return f"{int(mn):,} – {int(mx):,} €".replace(",", " ")
    if mn or mx:
        return f"{int(mn or mx):,} €".replace(",", " ")
    return "N/A"


def chercher_adzuna(requete):
    params = {
        "app_id": os.getenv("ADZUNA_APP_ID"),
        "app_key": os.getenv("ADZUNA_APP_KEY"),
        "results_per_page": RESULTATS_PAR_REQUETE,
        "what": requete,
        "max_days_old": ADZUNA_MAX_JOURS,   # [CORRECTIF] évite de renoter les mêmes offres
        "content-type": "application/json",
    }
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


_ft_token = None
_ft_avertissement_affiche = False


def token_france_travail():
    global _ft_token
    if _ft_token:
        return _ft_token
    r = requests.post(
        "https://entreprise.francetravail.fr/connexion/oauth2/access_token",
        params={"realm": "/partenaire"},
        data={"grant_type": "client_credentials", "client_id": FT_CLIENT_ID,
              "client_secret": FT_CLIENT_SECRET, "scope": "api_offresdemploiv2 o2dsoffre"},
        timeout=20)
    r.raise_for_status()
    _ft_token = r.json()["access_token"]
    return _ft_token


def chercher_france_travail(requete):
    global _ft_avertissement_affiche
    if not (FT_CLIENT_ID and FT_CLIENT_SECRET):
        # [CORRECTIF] signale la désactivation au lieu de renvoyer une liste vide en silence
        if not _ft_avertissement_affiche:
            print("ℹ️  France Travail désactivé : FT_CLIENT_ID / FT_CLIENT_SECRET absents")
            _ft_avertissement_affiche = True
        return []
    offres = []
    for dep in FT_DEPARTEMENTS:
        try:
            r = requests.get(
                "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search",
                headers={"Authorization": f"Bearer {token_france_travail()}",
                         "Accept": "application/json"},
                params={"motsCles": requete, "departement": dep,
                        "typeContrat": FT_TYPE_CONTRAT,
                        "publieeDepuis": FT_PUBLIEE_DEPUIS,
                        "range": f"0-{FT_RESULTATS_PAR_REQUETE - 1}"},
                timeout=20)
            if r.status_code == 204:      # aucun résultat
                continue
            r.raise_for_status()           # 200 ou 206 (résultats partiels) sont valides
            resultats = r.json().get("resultats", [])
        except Exception as e:
            print(f"⚠️  France Travail en erreur pour « {requete} » ({dep}) : {e}")
            continue
        for o in resultats:
            lieu = o.get("lieuTravail") or {}
            fid = str(o.get("id", ""))
            offres.append({
                "id": f"ft-{fid}",
                "titre": (o.get("intitule") or "").strip(),
                "entreprise": ((o.get("entreprise") or {}).get("nom") or "").strip(),
                "lieu_brut": lieu.get("libelle", ""),
                "lat": lieu.get("latitude"),
                "lon": lieu.get("longitude"),
                "description": nettoyer(o.get("description", "")),
                "salaire": (o.get("salaire") or {}).get("libelle") or "N/A",
                "lien": f"https://candidat.francetravail.fr/offres/recherche/detail/{fid}",
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


def enregistrer_ecartees(offres):
    """[AJOUT] Mémorise les offres sous le seuil (titre, entreprise, score, lien, date)."""
    if not offres:
        return
    if not DB_ECARTEES:
        print(f"ℹ️  {len(offres)} offre(s) écartée(s) non mémorisée(s) : NOTION_DB_ECARTEES absent")
        return
    if DRY_RUN:
        print(f"🗂️  DRY_RUN : {len(offres)} offre(s) écartée(s) seraient mémorisées")
        return
    ok = 0
    for o in offres:
        try:
            notion.pages.create(parent={"database_id": DB_ECARTEES}, properties={
                "Titre du poste": {"title": [{"text": {"content": o["titre"][:2000]}}]},
                "Entreprise": rt(o["entreprise"]),
                "Score": {"number": o["score"]},
                "Lien": {"url": o["lien"] or None},
                "Date": {"date": {"start": str(date.today())}},
            })
            ok += 1
        except Exception as e:
            print(f"   ↳ ⚠️ offre écartée non mémorisée ({o['titre'][:40]}) : {e}")
        time.sleep(PAUSE_NOTION)
    print(f"🗂️  {ok}/{len(offres)} offre(s) écartée(s) mémorisée(s)")


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
        noter_lot(offres[i:i + TAILLE_LOT_NOTATION], type_poste)


def noter_lot(lot, type_poste):
    compact = [{"id": o["id"], "titre": o["titre"], "entreprise": o["entreprise"],
                "lieu": o["lieu_brut"], "salaire": o["salaire"],
                "description": o["description"][:LONGUEUR_DESCRIPTION]} for o in lot]
    response = claude.messages.create(
        model=MODELE_TRI,
        max_tokens=8000,   # marge pour la réflexion du modèle, qui compte dans cette limite
        system=(
            "Tu évalues l'adéquation entre des offres et un profil. "
            "Note chaque offre de 0 à 100 : intitulé et niveau de séniorité, compétences, "
            "localisation (proche de Mougins/Cannes–Monaco ou remote), salaire si indiqué. "
            "Ne suppose rien qui n'est pas écrit dans l'offre.\n\n"
            f"Profil :\n{MON_PROFIL}"
        ),
        messages=[{"role": "user", "content":
            f"Type : {type_poste}\nOffres :\n{json.dumps(compact, ensure_ascii=False)}\n\n"
            'Réponds uniquement par un tableau JSON compact, sans commentaire : '
            '[{"id": "...", "score": 0}]'}],
    )
    compter(response)
    notes = extraire_json(texte_reponse(response), "[", "]") or []
    par_id = {str(n.get("id")): n.get("score") for n in notes if isinstance(n, dict)}
    manquantes = [o for o in lot if not isinstance(par_id.get(o["id"]), (int, float))]

    if manquantes and len(lot) > 1:
        print(f"⚠️  {len(manquantes)}/{len(lot)} offre(s) non notée(s) "
              f"(arrêt : {response.stop_reason}) → nouvel essai en lots plus petits")
        for o in lot:
            if o not in manquantes:
                o["score"] = int(par_id[o["id"]])
        moitie = max(1, len(manquantes) // 2)
        noter_lot(manquantes[:moitie], type_poste)
        if manquantes[moitie:]:
            noter_lot(manquantes[moitie:], type_poste)
        return

    for o in lot:
        s = par_id.get(o["id"])
        o["score"] = int(s) if isinstance(s, (int, float)) else 0
        # [AJOUT] un échec de notation n'est pas une vraie note : on ne la mémorise pas
        o["echec_notation"] = not isinstance(s, (int, float))
    if manquantes:
        print(f"⚠️  offre non notée après nouvel essai (score 0) : {lot[0]['titre']}")


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
    # [AJOUT] les offres déjà écartées comptent aussi comme « déjà vues »
    ids_ec, cles_ec, _ = charger_deja_vus(DB_ECARTEES, "Titre du poste")
    ids_vus |= ids_ec
    cles_vues |= cles_ec
    adzuna = [o for q in REQUETES_SALARIAT for o in chercher_adzuna(q)]
    ft = [o for q in REQUETES_SALARIAT for o in chercher_france_travail(q)]
    brutes = adzuna + ft
    offres = nouvelles_offres(brutes, ids_vus, cles_vues)
    print(f"🔎 Salariat : {len(adzuna)} résultats Adzuna, {len(ft)} France Travail, "
          f"{len(offres)} nouvelles offres à noter")
    if not offres:
        return
    noter_offres(offres, "CDI / salariat")
    meilleures = sorted(offres, key=lambda o: o["score"], reverse=True)[:5]
    print("🏅 Meilleurs scores : " + " | ".join(f"{o['score']} {o['titre'][:40]}" for o in meilleures))
    retenues = sorted([o for o in offres if o["score"] >= SEUIL_SAUVEGARDE],
                      key=lambda o: o["score"], reverse=True)
    enregistrer_ecartees([o for o in offres
                          if o["score"] < SEUIL_SAUVEGARDE and not o.get("echec_notation")])
    if len(retenues) > MAX_CONTACTS_PAR_JOUR:
        print(f"ℹ️  {len(retenues) - MAX_CONTACTS_PAR_JOUR} offre(s) reportée(s) à demain (plafond)")
        retenues = retenues[:MAX_CONTACTS_PAR_JOUR]

    cache = dict(contacts_connus)
    for o in retenues:
        print(f"👤 {o['score']:>3} | {o['titre']} – {o['entreprise'] or 'entreprise non précisée'}")
        connu = cache.get((o["entreprise"] or "").lower())
        try:
            if not o["entreprise"]:
                # [CORRECTIF] offre anonyme : pas de recherche de contact (coûteuse et vaine)
                vide = {"nom": None, "poste": None, "linkedin": None, "source": None,
                        "fiabilite": "Non trouvé"}
                enregistrer_offre(o, vide, "")
                continue
            if connu:
                contact, message = connu, rediger_message_seul(o, connu)
            else:
                contact, message = chercher_contact_et_message(o)
                if contact["fiabilite"] == "Vérifié" and o["entreprise"]:
                    cache[o["entreprise"].lower()] = contact
            enregistrer_offre(o, contact, raccourcir(message))
        except Exception as e:
            print(f"   ↳ ❌ erreur, offre non enregistrée (réessayée au prochain passage) : {e}")


def lancer_agent():
    print("🤖 Le robot démarre..." + (" (DRY_RUN : rien n'est écrit dans Notion)" if DRY_RUN else ""))
    partie_salariat()
    print(f"\n📊 Consommation : {CONSO['appels']} appels, {CONSO['input']} tokens en entrée, "
          f"{CONSO['output']} en sortie, {CONSO['recherches_web']} recherches web")
    print("✅ Terminé ! Regarde ton Notion.")


if __name__ == "__main__":
    lancer_agent()
