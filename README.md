# Agent de recherche d'emploi

Robot qui, chaque matin de semaine, collecte des offres d'emploi, les note par rapport à mon profil, puis enregistre les meilleures dans Notion avec un contact et un message LinkedIn prêt à envoyer.

## Fonctionnement

1. **Collecte** des offres via les sources :
   - **Adzuna** : France entière, ce qui couvre le remote.
2. **Dédoublonnage** en Python, par identifiant d'offre et par couple titre + entreprise, contre les offres déjà présentes dans Notion.
3. **Notation** de 0 à 100 par Claude Haiku, par lots de 25 offres.
4. **Recherche de contact** par Claude Sonnet avec recherche web, uniquement pour les offres notées 60 ou plus. Le plafond est de 15 offres par jour.
5. **Enregistrement** dans la base Notion « Offres d'emploi ».

Le principe : tout ce que Python sait faire est fait en Python, et Claude n'intervient que pour noter et rédiger, afin de limiter le coût.

### Règle anti-hallucination

Un contact n'est enregistré que si la page source a réellement été renvoyée par la recherche web. Dans le cas contraire, le contact est marqué « Non trouvé ». Le message LinkedIn n'utilise que des faits présents dans `profil.py`.

## Structure du dépôt

| Fichier | Rôle |
|---|---|
| 'agent.py' | Agent complet : collecte, notation, contacts, Notion |
| `profil.py` | Variable `MON_PROFIL` : mon profil, utilisé pour la notation et les messages |
| `.github/workflows/main.yml` | Exécution planifiée par GitHub Actions |

## Installation

### 1. Dépendances

```bash
pip install anthropic requests python-dotenv notion-client
```

### 2. Secrets GitHub

À créer dans *Settings → Secrets and variables → Actions* :

| Secret | Usage |
|---|---|
| `ANTHROPIC_API_KEY` | API Claude |
| `NOTION_API_KEY` | Intégration Notion |
| `NOTION_DB_OFFRES` | Identifiant de la base « Offres d'emploi » |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | API Adzuna |

Ces secrets doivent être transmis au script dans le bloc `env:` de `main.yml`.

Pour exécuter le script en local, place les mêmes variables dans un fichier `.env`. Ce fichier ne doit jamais être commité.

### 3. Accès France Travail --> L'API est désactivée pour le moment

1. Crée un compte sur [francetravail.io](https://francetravail.io).
2. Déclare une application.
3. Abonne cette application à **API Offres d'emploi v2**.
4. Récupère l'identifiant client et la clé secrète, puis renseigne-les dans les secrets.

### 4. Base Notion « Offres d'emploi »

Propriétés attendues, avec ces noms exacts :

| Propriété | Type |
|---|---|
| Titre du poste | Titre |
| Entreprise, Salaire, Mission, Lieu | Texte |
| Score | Nombre |
| Lien | URL |
| Date | Date |
| Contact – Nom, Contact – Poste | Texte |
| Contact – LinkedIn, Contact – Source | URL |
| Contact – Fiabilité | Sélection : `Vérifié`, `À vérifier`, `Non trouvé` |
| Message LinkedIn | Texte |

L'intégration Notion doit avoir accès à cette base.

## Configuration

Les réglages se trouvent en tête du script principal :

| Paramètre | Valeur | Rôle |
|---|---|---|
| `REQUETES_SALARIAT` | 4 intitulés | Mots-clés de recherche |
| `SEUIL_SAUVEGARDE` | 60 | Score minimum pour enregistrer une offre |
| `MAX_CONTACTS_PAR_JOUR` | 15 | Plafond de recherches de contact par exécution |
| `RECHERCHES_WEB_PAR_OFFRE` | 3 | Plafond de recherches web par offre |
| `FT_DEPARTEMENTS` | `["06"]` | Départements interrogés sur France Travail |
| `FT_TYPE_CONTRAT` | `"CDI"` | Type de contrat sur France Travail |
| `ADZUNA_MAX_JOURS` / `FT_PUBLIEE_DEPUIS` | 2 / 1 jour, plus le lundi | Ancienneté maximale des offres |

Le lundi, les fenêtres d'ancienneté sont élargies pour couvrir le week-end, puisque le robot ne tourne qu'en semaine.

## Tester sans écrire dans Notion

Définis `DRY_RUN=1`. Les offres qui seraient enregistrées s'affichent alors dans les logs, sans être écrites dans Notion.

```bash
DRY_RUN=1 python <script_principal>.py
```

## Lire les logs

| Ligne | Signification |
|---|---|
| `📚 N lignes déjà en base` | Lecture Notion réussie, base du dédoublonnage |
| `🔎 Salariat : X Adzuna, Y France Travail, Z nouvelles` | Volume collecté, puis volume restant après dédoublonnage |
| `🏅 Meilleurs scores` | Les 5 meilleures notes du jour |
| `👤 score \| titre – entreprise` | Offre retenue, en cours de traitement |
| `📊 Consommation` | Appels Claude, tokens consommés et recherches web |
| `⚠️` / `❌` | Erreur sur une source ou sur une offre. L'exécution continue malgré tout. |

## Limites connues

- Une offre notée sous 60 n'est pas mémorisée. Elle peut donc être renotée tant qu'elle reste dans la fenêtre d'ancienneté.
- Une offre publiée sur les deux sources sous des noms d'entreprise différents peut apparaître en double.
- Si une exécution échoue, les offres de ce jour-là peuvent être manquées, car la fenêtre d'ancienneté est courte.
