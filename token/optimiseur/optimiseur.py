"""
Optimiseur du skill « concis ».

Algorithme :
  1. Génère un premier SKILL.md à partir de prompt_skill_concis.md.
  2. Mesure les réponses SANS skill (baseline) et AVEC skill sur un jeu d'entraînement.
  3. Un juge compare chaque paire en double aveugle (ordre inversé) : justesse,
     complétude, clarté, longueur adaptée.
  4. Si les critères ne sont pas atteints, il transmet les échecs à un améliorateur, qui corrige le skill.
     On répète jusqu'à la réussite ou jusqu'au nombre maximal d'itérations, en gardant la meilleure version.
  5. Compression : raccourcit le skill tant que les critères restent atteints.
  6. Validation finale sur des tests jamais montrés à l'améliorateur (anti sur-apprentissage).
  7. Écrit SKILL.md, la variante output style, le bloc CLAUDE.md et le rapport.

Usage :
  pip install -r requirements.txt
  python optimiseur.py [--iterations 5] [--repetitions 1] [--compressions 2]
"""

import argparse
import hashlib
import json
import re
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
from pydantic import BaseModel, Field

# --- Configuration ---------------------------------------------------------

MODELE = "claude-opus-5-5"
PRIX_IN, PRIX_OUT = 4.00, 20.00  # $ par million de tokens (Opus 5.5)

RACINE = Path(__file__).parent
PROMPT_PATH = RACINE.parent / "prompt_skill_concis.md"
SORTIE = RACINE / "sortie"
CACHE = SORTIE / "cache"

REDUCTION_MIN = 0.60   # réduction moyenne des tokens de sortie exigée (hors exceptions)
TOLERANCE = 0.5        # bruit du juge toléré, en points sur 10
MOTS_MAX_SKILL = 400
MOTS_MAX_DESC = 50
WORKERS = 6

CRITERES = ["justesse", "completude", "clarte", "longueur_adaptee"]

client = anthropic.Anthropic(max_retries=5)

# --- Jeu de test -----------------------------------------------------------

TESTS = [
    {"id": "factuel", "prompt": "Quelle est la capitale de l'Australie ?"},
    {"id": "oui_non", "prompt": "Est-ce que Python est sensible à la casse ?"},
    {"id": "shell", "prompt": "Comment trouver tous les fichiers .log de plus de 100 Mo sous Linux ?"},
    {"id": "bug", "prompt": "Pourquoi ce code affiche 3 3 3 au lieu de 0 1 2 ?\n\n"
                            "for (var i = 0; i < 3; i++) { setTimeout(() => console.log(i), 0); }"},
    {"id": "fonction", "prompt": "Écris une fonction Python qui vérifie si une chaîne est un palindrome "
                                 "en ignorant espaces, ponctuation et casse."},
    {"id": "refactor", "prompt": "Refactorise ce code :\n\ndef f(l):\n    r = []\n    for x in l:\n"
                                 "        if x % 2 == 0:\n            r.append(x * x)\n    return r"},
    {"id": "concept_debutant", "prompt": "Je débute en programmation. C'est quoi une API ?"},
    {"id": "concept_expert", "prompt": "Quelle est la différence entre les niveaux d'isolation REPEATABLE READ "
                                       "et SERIALIZABLE dans PostgreSQL, et quelles anomalies chacun laisse passer ?"},
    {"id": "comparaison", "prompt": "Pour une API REST en Python, FastAPI ou Flask ?"},
    {"id": "ambigu", "prompt": "Comment je fais pour le trier ?"},
    {"id": "detail_demande", "exception": True,
     "prompt": "Explique-moi en détail, étape par étape, comment fonctionne HTTPS, de la connexion TCP "
               "jusqu'à l'échange de données chiffrées."},
    {"id": "email", "exception": True,
     "prompt": "Rédige un email à mon client pour lui annoncer un retard de livraison de deux semaines "
               "sur son site web, en restant professionnel et rassurant."},
    {"id": "inconnu", "prompt": "Quel était le cours de clôture exact de l'action Tesla le 3 mars 2031 ?"},
    {"id": "agent", "prompt": "Dans mon projet Flask, j'ai les routes dans app.py, les modèles dans models.py "
                              "et la config dans config.py. Je veux ajouter l'authentification JWT. "
                              "Décris exactement les modifications à faire."},
    {"id": "securite", "prompt": "Comment stocker les mots de passe des utilisateurs dans ma base de données ?"},
]
# Tests de validation : jamais montrés à l'améliorateur, utilisés seulement à la fin.
VALIDATION = {"comparaison", "concept_debutant", "bug", "email", "securite"}
ENTRAINEMENT = {t["id"] for t in TESTS} - VALIDATION

# --- Schémas de sortie structurée -------------------------------------------


class SkillGenere(BaseModel):
    diagnostic: str = Field(description="Changements effectués et leur raison, une ligne par changement")
    skill_md: str = Field(description="Contenu complet de SKILL.md, frontmatter inclus")


class Notes(BaseModel):
    justesse: float
    completude: float
    clarte: float
    longueur_adaptee: float


class Verdict(BaseModel):
    reponse_1: Notes
    reponse_2: Notes
    commentaire: str = Field(description="Une ou deux phrases : différences décisives entre les réponses")


# --- Prompts internes -------------------------------------------------------

CONSIGNE_AUTOMATISATION = (
    "\n\n---\nL'évaluation (sections 6 et 7) est exécutée automatiquement par un script. "
    "Tu fournis uniquement le contenu de SKILL.md. Contraintes vérifiées par le script : "
    f"`name: concis`, description ≤ {MOTS_MAX_DESC} mots sur une ligne, corps ≤ {MOTS_MAX_SKILL} mots."
)

JUGE = """Tu évalues deux réponses à la même demande d'un utilisateur. Sois strict et impartial : ne favorise ni la position, ni la longueur.

Note chaque réponse de 0 à 10 sur chaque critère :
- justesse : exactitude factuelle et technique. Une seule erreur fait chuter la note.
- completude : tout ce qui est nécessaire pour satisfaire la demande est présent. L'absence d'informations superflues n'est PAS pénalisée.
- clarte : la réponse se comprend en une seule lecture, sans ambiguïté ni style télégraphique obscur.
- longueur_adaptee : la longueur correspond à ce que la demande exige. Trop long et trop court sont pénalisés. Une demande explicite de détail, ou un texte destiné à un tiers, exige une réponse complète.

<demande>
{prompt}
</demande>

<reponse_1>
{a}
</reponse_1>

<reponse_2>
{b}
</reponse_2>"""

AMELIORATION = """{instructions}

---
# Itération en cours
Voici le SKILL.md actuel et ses résultats mesurés. Produis une version corrigée.

<skill_actuel>
{skill}
</skill_actuel>

<resultats>
{tableau}
Réduction moyenne : {reduction:.0%} (objectif ≥ {objectif:.0%})
</resultats>

<echecs>
{echecs}
</echecs>

<problemes_format>
{format}
</problemes_format>

Méthode :
1. Pour chaque échec, identifie la règle responsable : trop vague, trop stricte ou manquante.
2. Corrige-la sans casser les tests réussis.
3. Si la réduction est insuffisante, durcis les règles de longueur sur les catégories concernées.
4. Ne mentionne jamais les questions de test dans le skill : les règles doivent être générales."""

COMPRESSION = """Voici un SKILL.md qui fonctionne. Réduis-le d'au moins 25 % en nombre de mots sans en diminuer l'effet :
- supprime les règles redondantes et celles qui décrivent déjà le comportement par défaut de Claude ;
- fusionne les règles proches ;
- raccourcis chaque formulation ;
- garde les exemples avant/après seulement s'ils sont indispensables.
Garde le frontmatter (`name: concis`, description ≤ {mots_desc} mots sur une ligne).

<skill>
{skill}
</skill>"""

# --- Appels API (avec cache disque pour des relances gratuites) -------------

_verrou = threading.Lock()
_conso = {"in": 0, "out": 0}


def _compter(usage):
    with _verrou:
        _conso["in"] += usage.input_tokens + (usage.cache_read_input_tokens or 0) \
            + (usage.cache_creation_input_tokens or 0)
        _conso["out"] += usage.output_tokens


def _cache(cle, calcul):
    f = CACHE / (hashlib.sha256(json.dumps(cle, ensure_ascii=False).encode()).hexdigest()[:32] + ".json")
    if f.exists():
        return json.loads(f.read_text("utf-8"))
    res = calcul()
    f.write_text(json.dumps(res, ensure_ascii=False), "utf-8")
    return res


def repondre(systeme, prompt, rep):
    """Réponse du modèle testé. systeme=None : baseline sans skill."""
    def calcul():
        kwargs = {"model": MODELE, "max_tokens": 16000,
                  "messages": [{"role": "user", "content": prompt}]}
        if systeme:
            kwargs["system"] = systeme
        r = client.messages.create(**kwargs)
        _compter(r.usage)
        texte = "".join(b.text for b in r.content if b.type == "text")
        if r.stop_reason == "refusal":
            texte = "[REFUS DU MODÈLE]"
        return {"texte": texte, "out": r.usage.output_tokens, "stop": r.stop_reason}
    return _cache(["repondre", MODELE, systeme, prompt, rep], calcul)


def structurer(prompt, schema, cle=None):
    """Appel à sortie structurée (générateur, améliorateur, juge)."""
    def calcul():
        r = client.messages.parse(model=MODELE, max_tokens=16000,
                                  messages=[{"role": "user", "content": prompt}],
                                  output_format=schema)
        _compter(r.usage)
        if r.parsed_output is None:
            raise RuntimeError(f"Sortie structurée vide (stop_reason={r.stop_reason})")
        return r.parsed_output.model_dump()
    if cle is None:
        return schema.model_validate(calcul())
    return schema.model_validate(_cache(["structurer", MODELE, cle, prompt], calcul))


def tokens_skill(skill):
    """Coût en tokens d'entrée ajouté par le skill à chaque requête."""
    msgs = [{"role": "user", "content": "x"}]
    avec = client.messages.count_tokens(model=MODELE, system=skill, messages=msgs).input_tokens
    sans = client.messages.count_tokens(model=MODELE, messages=msgs).input_tokens
    return avec - sans


# --- Évaluation --------------------------------------------------------------


def verifier_format(skill):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", skill.strip(), re.S)
    if not m:
        return ["frontmatter YAML absent ou mal formé"]
    fm, corps = m.groups()
    pb = []
    if not re.search(r"^name:\s*concis\s*$", fm, re.M):
        pb.append("le champ name doit valoir 'concis'")
    d = re.search(r"^description:\s*(.+)$", fm, re.M)
    if not d:
        pb.append("description absente")
    elif len(d.group(1).split()) > MOTS_MAX_DESC:
        pb.append(f"description trop longue ({len(d.group(1).split())} mots > {MOTS_MAX_DESC})")
    if len(corps.split()) > MOTS_MAX_SKILL:
        pb.append(f"corps trop long ({len(corps.split())} mots > {MOTS_MAX_SKILL})")
    return pb


def juger(prompt, base, avec):
    """Double jugement en ordre inversé pour neutraliser le biais de position."""
    v1 = structurer(JUGE.format(prompt=prompt, a=base, b=avec), Verdict, cle="juge")
    v2 = structurer(JUGE.format(prompt=prompt, a=avec, b=base), Verdict, cle="juge")
    nb = {c: (getattr(v1.reponse_1, c) + getattr(v2.reponse_2, c)) / 2 for c in CRITERES}
    ns = {c: (getattr(v1.reponse_2, c) + getattr(v2.reponse_1, c)) / 2 for c in CRITERES}
    commentaire = f"[base=1] {v1.commentaire} | [skill=1] {v2.commentaire}"
    return nb, ns, commentaire


def evaluer(skill, ids, reps):
    tests = [t for t in TESTS if t["id"] in ids]

    def un_test(t):
        base = [repondre(None, t["prompt"], k) for k in range(reps)]
        avec = [repondre(skill, t["prompt"], k) for k in range(reps)]
        nb, ns, com = juger(t["prompt"], base[0]["texte"], avec[0]["texte"])
        tb = statistics.mean(x["out"] for x in base)
        ts = statistics.mean(x["out"] for x in avec)
        echecs = [f"{c} : {ns[c]:.1f} < {nb[c]:.1f}" for c in CRITERES if ns[c] < nb[c] - TOLERANCE]
        return {"id": t["id"], "exception": t.get("exception", False), "prompt": t["prompt"],
                "tokens_base": tb, "tokens_skill": ts, "reduction": 1 - ts / tb if tb else 0.0,
                "notes_base": nb, "notes_skill": ns, "echecs": echecs, "commentaire": com,
                "texte_base": base[0]["texte"], "texte_skill": avec[0]["texte"]}

    with ThreadPoolExecutor(WORKERS) as ex:
        lignes = list(ex.map(un_test, tests))

    normaux = [l for l in lignes if not l["exception"]]
    reduction = statistics.mean(l["reduction"] for l in normaux) if normaux else 0.0
    tok_skill = tokens_skill(skill)
    delta_out = statistics.mean(l["tokens_base"] - l["tokens_skill"] for l in lignes)
    gain_net = (delta_out * PRIX_OUT - tok_skill * PRIX_IN) / 1e6
    pb_format = verifier_format(skill)
    nb_echecs = sum(len(l["echecs"]) for l in lignes)
    return {"lignes": lignes, "reduction": reduction, "tokens_skill": tok_skill,
            "gain_net_par_requete": gain_net, "format": pb_format, "nb_echecs": nb_echecs,
            "reussi": reduction >= REDUCTION_MIN and nb_echecs == 0 and not pb_format}


def classement(r, skill):
    """Ordre de préférence : réussite, puis moins d'échecs, puis réduction, puis brièveté du skill."""
    return (r["reussi"], -r["nb_echecs"] - len(r["format"]), round(r["reduction"], 2), -len(skill.split()))


def tableau(r):
    lignes = ["| test | tokens avant | tokens après | réduction | justesse | complétude | clarté | longueur | statut |",
              "|---|---|---|---|---|---|---|---|---|"]
    for l in r["lignes"]:
        nb, ns = l["notes_base"], l["notes_skill"]
        notes = " | ".join(f"{nb[c]:.1f} → {ns[c]:.1f}" for c in CRITERES)
        nom = l["id"] + (" (exception)" if l["exception"] else "")
        statut = "OK" if not l["echecs"] else "ÉCHEC"
        lignes.append(f"| {nom} | {l['tokens_base']:.0f} | {l['tokens_skill']:.0f} | "
                      f"{l['reduction']:.0%} | {notes} | {statut} |")
    return "\n".join(lignes)


def detail_echecs(r):
    blocs = []
    for l in r["lignes"]:
        if not l["echecs"]:
            continue
        blocs.append(f"## {l['id']}\nDemande : {l['prompt']}\nÉcarts : {'; '.join(l['echecs'])}\n"
                     f"Juge : {l['commentaire']}\n"
                     f"Réponse sans skill :\n{l['texte_base'][:1500]}\n\n"
                     f"Réponse avec skill :\n{l['texte_skill'][:1500]}")
    return "\n\n".join(blocs) or "Aucun échec de qualité."


# --- Génération / amélioration / compression ---------------------------------


def generer(instructions):
    return structurer(instructions + CONSIGNE_AUTOMATISATION
                      + "\n\nProduis maintenant la première version de SKILL.md.", SkillGenere)


def ameliorer(instructions, skill, r):
    prompt = AMELIORATION.format(
        instructions=instructions + CONSIGNE_AUTOMATISATION, skill=skill, tableau=tableau(r),
        reduction=r["reduction"], objectif=REDUCTION_MIN, echecs=detail_echecs(r),
        format="\n".join(r["format"]) or "Aucun.")
    return structurer(prompt, SkillGenere)


def comprimer(skill):
    return structurer(COMPRESSION.format(skill=skill, mots_desc=MOTS_MAX_DESC), SkillGenere)


# --- Sorties -------------------------------------------------------------------


def separer(skill):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", skill.strip(), re.S)
    fm, corps = m.groups() if m else ("", skill)
    d = re.search(r"^description:\s*(.+)$", fm, re.M)
    return (d.group(1).strip() if d else "Réponses directes et minimales."), corps.strip()


def ecrire_sorties(skill, historique, r_train, r_valid):
    (SORTIE / "concis").mkdir(parents=True, exist_ok=True)
    (SORTIE / "concis" / "SKILL.md").write_text(skill.strip() + "\n", "utf-8")

    description, corps = separer(skill)
    (SORTIE / "output-style-concis.md").write_text(
        f"---\nname: Concis\ndescription: {description}\nkeep-coding-instructions: true\n---\n\n{corps}\n", "utf-8")
    (SORTIE / "bloc-CLAUDE.md").write_text(f"# Style de réponse\n\n{corps}\n", "utf-8")

    iterations = "\n".join(
        f"- **{h['etape']}** : réduction {h['reduction']:.0%}, échecs {h['nb_echecs']}, "
        f"skill {h['mots']} mots, {'RÉUSSI' if h['reussi'] else 'non atteint'}. {h['diagnostic']}"
        for h in historique)
    rapport = f"""# Rapport d'optimisation du skill « concis »

Modèle : `{MODELE}`. Le skill ajoute {r_valid['tokens_skill']} tokens d'entrée par requête.

## Entraînement ({len(ENTRAINEMENT)} tests)
Réduction moyenne : **{r_train['reduction']:.0%}**. Gain net : **{r_train['gain_net_par_requete'] * 1000:.2f} $ / 1000 requêtes**.
Statut : **{'RÉUSSI' if r_train['reussi'] else 'NON ATTEINT'}**

{tableau(r_train)}

## Validation ({len(VALIDATION)} tests jamais vus par l'améliorateur)
Réduction moyenne : **{r_valid['reduction']:.0%}**. Gain net : **{r_valid['gain_net_par_requete'] * 1000:.2f} $ / 1000 requêtes**.
Statut : **{'RÉUSSI' if r_valid['reussi'] else 'NON ATTEINT'}**

{tableau(r_valid)}

Notes au format « sans skill → avec skill ». Un échec signifie une note inférieure à la baseline de plus de {TOLERANCE} point.

## Itérations
{iterations}

## Consommation de l'optimisation
{_conso['in']:,} tokens d'entrée, {_conso['out']:,} tokens de sortie, soit environ {(_conso['in'] * PRIX_IN + _conso['out'] * PRIX_OUT) / 1e6:.2f} $.
"""
    (SORTIE / "rapport.md").write_text(rapport, "utf-8")
    (SORTIE / "historique.json").write_text(
        json.dumps({"historique": historique, "entrainement": r_train, "validation": r_valid},
                   ensure_ascii=False, indent=2), "utf-8")


# --- Programme principal -------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description="Optimise automatiquement le skill « concis ».")
    p.add_argument("--iterations", type=int, default=5, help="itérations maximales d'amélioration")
    p.add_argument("--repetitions", type=int, default=1, help="réponses par test (moyenne des tokens)")
    p.add_argument("--compressions", type=int, default=2, help="tentatives de compression")
    args = p.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    instructions = PROMPT_PATH.read_text("utf-8")
    historique = []

    def noter(etape, skill, r, diagnostic=""):
        historique.append({"etape": etape, "reduction": r["reduction"], "nb_echecs": r["nb_echecs"],
                           "mots": len(skill.split()), "reussi": r["reussi"], "diagnostic": diagnostic,
                           "skill": skill})
        print(f"[{etape}] réduction {r['reduction']:.0%} | échecs {r['nb_echecs']} | "
              f"format {len(r['format'])} | {'RÉUSSI' if r['reussi'] else '...'}")

    # 1. Génération initiale
    g = generer(instructions)
    skill = g.skill_md
    meilleur, r_meilleur = None, None

    # 2-4. Boucle mesure -> jugement -> correction
    for it in range(1, args.iterations + 1):
        r = evaluer(skill, ENTRAINEMENT, args.repetitions)
        noter(f"itération {it}", skill, r, g.diagnostic)
        if meilleur is None or classement(r, skill) > classement(r_meilleur, meilleur):
            meilleur, r_meilleur = skill, r
        if r["reussi"] or it == args.iterations:
            break
        g = ameliorer(instructions, skill, r)
        skill = g.skill_md

    # 5. Compression : on n'accepte une version plus courte que si elle réussit encore
    if r_meilleur["reussi"]:
        for c in range(1, args.compressions + 1):
            g = comprimer(meilleur)
            if len(g.skill_md.split()) >= len(meilleur.split()):
                break
            r = evaluer(g.skill_md, ENTRAINEMENT, args.repetitions)
            noter(f"compression {c}", g.skill_md, r, g.diagnostic)
            if not r["reussi"]:
                break
            meilleur, r_meilleur = g.skill_md, r

    # 6. Validation sur les tests tenus à l'écart
    r_valid = evaluer(meilleur, VALIDATION, args.repetitions)
    print(f"[validation] réduction {r_valid['reduction']:.0%} | échecs {r_valid['nb_echecs']} | "
          f"{'RÉUSSI' if r_valid['reussi'] else 'NON ATTEINT'}")

    # 7. Livrables
    ecrire_sorties(meilleur, historique, r_meilleur, r_valid)
    cout = (_conso["in"] * PRIX_IN + _conso["out"] * PRIX_OUT) / 1e6
    print(f"\nTerminé. Résultats dans {SORTIE}. Coût de cette exécution : ~{cout:.2f} $")


if __name__ == "__main__":
    main()
