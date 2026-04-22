import os, json
from datetime import date
from dotenv import load_dotenv
import anthropic, requests
from notion_client import Client

load_dotenv()
from profil import MON_PROFIL

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
notion = Client(auth=os.getenv("NOTION_API_KEY"))

MOUGINS_LAT = 43.6000
MOUGINS_LON = 7.0333

def calculer_distance(ville):
    try:
        r = requests.get(
            "https://api-adresse.data.gouv.fr/search/",
            params={"q": ville, "limit": 1}
        )
        data = r.json()
        if not data["features"]:
            return None
        coords = data["features"][0]["geometry"]["coordinates"]
        lon, lat = coords[0], coords[1]
        from math import radians, sin, cos, sqrt, atan2
        R = 6371
        dlat = radians(lat - MOUGINS_LAT)
        dlon = radians(lon - MOUGINS_LON)
        a = sin(dlat/2)**2 + cos(radians(MOUGINS_LAT)) * cos(radians(lat)) * sin(dlon/2)**2
        return round(R * 2 * atan2(sqrt(a), sqrt(1-a)))
    except:
        return None

def formater_lieu(lieu_brut):
    if not lieu_brut:
        return "Non précisé"
    lieu_lower = lieu_brut.lower()
    if any(mot in lieu_lower for mot in ["remote", "télétravail", "teletravail", "distanciel", "full remote"]):
        return "Remote 🌐"
    distance = calculer_distance(lieu_brut)
    if distance is not None:
        return f"{lieu_brut} ({distance} km de Mougins)"
    return lieu_brut

tools = [
    {
        "name": "chercher_offres",
        "description": "Cherche des offres d'emploi salariat sur internet",
        "input_schema": {
            "type": "object",
            "properties": {
                "metier": {"type": "string"}
            },
            "required": ["metier"]
        }
    },
    {
        "name": "chercher_missions_freelance",
        "description": "Cherche des missions freelance sur Malt, Comet, Freelance.com",
        "input_schema": {
            "type": "object",
            "properties": {
                "metier": {"type": "string"}
            },
            "required": ["metier"]
        }
    },
    {
        "name": "sauvegarder_offre",
        "description": "Sauvegarde une offre salariat dans Notion",
        "input_schema": {
            "type": "object",
            "properties": {
                "titre":      {"type": "string"},
                "entreprise": {"type": "string"},
                "score":      {"type": "number"},
                "salaire":    {"type": "string"},
                "mission":    {"type": "string"},
                "lien":       {"type": "string"},
                "lieu":       {"type": "string"}
            },
            "required": ["titre", "entreprise", "score", "mission"]
        }
    },
    {
        "name": "sauvegarder_mission_freelance",
        "description": "Sauvegarde une mission freelance dans Notion",
        "input_schema": {
            "type": "object",
            "properties": {
                "titre":       {"type": "string"},
                "client":      {"type": "string"},
                "score":       {"type": "number"},
                "tjm":         {"type": "string", "description": "Taux Journalier Moyen ex: 600-800€/jour"},
                "duree":       {"type": "string", "description": "ex: 3 mois, 6 mois"},
                "lieu":        {"type": "string"},
                "mission":     {"type": "string"},
                "lien":        {"type": "string"},
                "plateforme":  {"type": "string", "description": "ex: Malt, Comet, Freelance.com"}
            },
            "required": ["titre", "score", "mission", "plateforme"]
        }
    },
    {
        "name": "sauvegarder_entreprise",
        "description": "Sauvegarde une fiche entreprise dans Notion",
        "input_schema": {
            "type": "object",
            "properties": {
                "nom":                  {"type": "string"},
                "secteur":              {"type": "string"},
                "contact":              {"type": "string"},
                "salaire_estime":       {"type": "string"},
                "resume":               {"type": "string"},
                "pourquoi_interessant": {"type": "string"}
            },
            "required": ["nom", "resume", "pourquoi_interessant"]
        }
    }
]

def executer_outil(nom, params):

    if nom == "chercher_offres":
        r = requests.get(
            "https://api.adzuna.com/v1/api/jobs/fr/search/1",
            params={
                "app_id":           os.getenv("ADZUNA_APP_ID"),
                "app_key":          os.getenv("ADZUNA_APP_KEY"),
                "results_per_page": 20,
                "what":             params["metier"],
                "content-type":     "application/json"
            }
        )
        return r.json()

    elif nom == "chercher_missions_freelance":
        # Recherche sur les 3 plateformes principales
        resultats = []

        # Freelance.com API (gratuite)
        try:
            r = requests.get(
                "https://www.freelance.com/api/projects/search",
                params={
                    "keywords": params["metier"],
                    "limit": 10
                },
                headers={"Accept": "application/json"}
            )
            if r.status_code == 200:
                resultats.append({"plateforme": "Freelance.com", "data": r.json()})
        except:
            pass

        # Adzuna pour les missions freelance/contract
        try:
            r = requests.get(
                "https://api.adzuna.com/v1/api/jobs/fr/search/1",
                params={
                    "app_id":           os.getenv("ADZUNA_APP_ID"),
                    "app_key":          os.getenv("ADZUNA_APP_KEY"),
                    "results_per_page": 10,
                    "what":             f"{params['metier']} freelance mission",
                    "contract":         1,
                    "content-type":     "application/json"
                }
            )
            resultats.append({"plateforme": "Adzuna Freelance", "data": r.json()})
        except:
            pass

        return resultats

    elif nom == "sauvegarder_offre":
        lieu_formate = formater_lieu(params.get("lieu", ""))
        notion.pages.create(
            parent={"database_id": os.getenv("NOTION_DB_OFFRES")},
            properties={
                "Titre du poste": {"title":     [{"text": {"content": params["titre"]}}]},
                "Entreprise":     {"rich_text": [{"text": {"content": params.get("entreprise","")}}]},
                "Score":          {"number":    params.get("score", 0)},
                "Salaire":        {"rich_text": [{"text": {"content": params.get("salaire","N/A")}}]},
                "Mission":        {"rich_text": [{"text": {"content": params.get("mission","")[:2000]}}]},
                "Lien":           {"url":       params.get("lien") or None},
                "Lieu":           {"rich_text": [{"text": {"content": lieu_formate}}]},
                "Date":           {"date":      {"start": str(date.today())}}
            }
        )
        return {"status": "ok"}

    elif nom == "sauvegarder_mission_freelance":
        lieu_formate = formater_lieu(params.get("lieu", "Remote"))
        notion.pages.create(
            parent={"database_id": os.getenv("NOTION_DB_FREELANCE")},
            properties={
                "Titre de la mission": {"title":     [{"text": {"content": params["titre"]}}]},
                "Client":              {"rich_text": [{"text": {"content": params.get("client","")}}]},
                "Score":               {"number":    params.get("score", 0)},
                "TJM proposé":         {"rich_text": [{"text": {"content": params.get("tjm","N/A")}}]},
                "Durée":               {"rich_text": [{"text": {"content": params.get("duree","N/A")}}]},
                "Lieu":                {"rich_text": [{"text": {"content": lieu_formate}}]},
                "Mission":             {"rich_text": [{"text": {"content": params.get("mission","")[:2000]}}]},
                "Lien":                {"url":       params.get("lien") or None},
                "Plateforme":          {"rich_text": [{"text": {"content": params.get("plateforme","")}}]},
                "Date":                {"date":      {"start": str(date.today())}}
            }
        )
        return {"status": "ok"}

    elif nom == "sauvegarder_entreprise":
        notion.pages.create(
            parent={"database_id": os.getenv("NOTION_DB_ENTREPRISES")},
            properties={
                "Entreprise":            {"title":     [{"text": {"content": params["nom"]}}]},
                "Secteur":               {"rich_text": [{"text": {"content": params.get("secteur","")}}]},
                "Contact":               {"rich_text": [{"text": {"content": params.get("contact","")}}]},
                "Salaire estimé":        {"rich_text": [{"text": {"content": params.get("salaire_estime","")}}]},
                "Résumé":                {"rich_text": [{"text": {"content": params.get("resume","")[:2000]}}]},
                "Pourquoi intéressant":  {"rich_text": [{"text": {"content": params.get("pourquoi_interessant","")[:2000]}}]},
                "Date":                  {"date":      {"start": str(date.today())}}
            }
        )
        return {"status": "ok"}

def lancer_agent():
    print("🤖 Le robot démarre...")

    messages = [{
        "role": "user",
        "content": f"""
Tu es un assistant qui cherche du travail pour quelqu'un.
Voici son profil :
{MON_PROFIL}

Ce que tu dois faire aujourd'hui :

PARTIE 1 - OFFRES SALARIAT :
1. Cherche des offres pour : "Lead Data Engineer", "Chef de projet data", "Senior Data Analyst"
2. Score chaque offre de 0 à 100
3. Sauvegarde uniquement les offres avec score >= 60
4. Indique le lieu exact ou "Remote"

PARTIE 2 - MISSIONS FREELANCE :
5. Cherche des missions freelance pour : "Data Engineer freelance", "Data Analyst mission"
6. Score chaque mission de 0 à 100
7. Sauvegarde uniquement les missions avec score >= 60
8. Indique le TJM, la durée et le lieu

PARTIE 3 - ENTREPRISES :
9. Trouve 3 entreprises qui pourraient recruter ce profil bientôt
10. Pour chaque entreprise : contact RH, résumé, salaire estimé
11. Sauvegarde dans Notion

Go !
"""
    }]

    while True:
        response = claude.messages.create(
            model="claude-opus-4-5",
            max_tokens=4096,
            tools=tools,
            messages=messages
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            print("✅ Terminé ! Regarde ton Notion.")
            break

        resultats = []
        for bloc in response.content:
            if bloc.type == "tool_use":
                print(f"🔧 Action : {bloc.name}")
                resultat = executer_outil(bloc.name, bloc.input)
                resultats.append({
                    "type":        "tool_result",
                    "tool_use_id": bloc.id,
                    "content":     json.dumps(resultat, ensure_ascii=False)
                })

        if resultats:
            messages.append({"role": "user", "content": resultats})
        else:
            break

if __name__ == "__main__":
    lancer_agent()
