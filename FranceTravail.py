"""
Source France Travail pour l'agent de recherche d'emploi.
À importer dans le script principal : from france_travail import fetch_france_travail
Secrets attendus (GitHub Actions) : FT_CLIENT_ID, FT_CLIENT_SECRET
"""
import os
import requests

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"


def _get_token():
    """Échange identifiant + clé secrète contre un jeton temporaire (OAuth2 client credentials)."""
    r = requests.post(
        TOKEN_URL,
        params={"realm": "/partenaire"},
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["FT_CLIENT_ID"],
            "client_secret": os.environ["FT_CLIENT_SECRET"],
            "scope": "api_offresdemploiv2 o2dsoffre",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_france_travail(mots_cles=("analytics engineer", "data engineer", "dbt", "data analyst")):
    """Renvoie une liste d'offres au même format que les offres Adzuna (à adapter à ton script)."""
    token = _get_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    offres, vus = [], set()

    for mc in mots_cles:
        params = {
            "motsCles": mc,
            "departement": "06",      # Alpes-Maritimes
            "typeContrat": "CDI",
            "publieeDepuis": 1,       # offres publiées depuis 1 jour (scan quotidien)
            "range": "0-149",         # 150 résultats max par appel
        }
        r = requests.get(SEARCH_URL, headers=headers, params=params, timeout=20)
        if r.status_code == 204:      # aucune offre
            continue
        r.raise_for_status()          # 200 ou 206 (résultats partiels) = OK

        for o in r.json().get("resultats", []):
            if o["id"] in vus:
                continue
            vus.add(o["id"])
            lieu = o.get("lieuTravail", {})
            offres.append({
                "titre": o.get("intitule", ""),
                "entreprise": (o.get("entreprise") or {}).get("nom", "Non précisé"),
                "lieu": lieu.get("libelle", ""),
                "latitude": lieu.get("latitude"),
                "longitude": lieu.get("longitude"),
                "description": o.get("description", ""),
                "salaire": (o.get("salaire") or {}).get("libelle", ""),
                "url": (o.get("origineOffre") or {}).get("urlOrigine")
                       or f"https://candidat.francetravail.fr/offres/recherche/detail/{o['id']}",
                "date": o.get("dateCreation", ""),
                "source": "France Travail",
            })
    return offres
