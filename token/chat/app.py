"""
Concis : interface de discussion avec Claude, économe en tokens.

Leviers d'économie :
  - un skill court par mode, plus des règles communes de concision (prompt système) ;
  - un effort de réflexion réglé par mode (low par défaut : moins de tokens de réflexion facturés) ;
  - un cache de prompt automatique (l'historique déjà envoyé est relu à environ 10 % du prix) ;
  - un historique en texte seul, avec les anciens échanges résumés automatiquement par un modèle léger ;
  - un titre de conversation sans appel à l'API (début du premier message).

Lancement :
  pip install -r requirements.txt
  python app.py            -> ouvre la fenêtre de l'application
"""

import hashlib
import io
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
import zipfile
from pathlib import Path

import anthropic
from flask import Flask, Response, abort, jsonify, request

import agent

if getattr(sys, "frozen", False):  # exécutable PyInstaller
    RESSOURCES = Path(sys._MEIPASS)        # fichiers intégrés (interface, modes par défaut)
    RACINE = Path(sys.executable).parent   # données modifiables, à côté du .exe
else:
    RESSOURCES = RACINE = Path(__file__).parent

MODES = RACINE / "modes"
MODES_DEFAUT = RESSOURCES / "modes"
BASE = MODES / "_base.md"
DONNEES = RACINE / "donnees"
CONVS = DONNEES / "conversations"
CONFIG = DONNEES / "config.json"

PRIX = {  # $ par million de tokens : (entrée, sortie)
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-4-8": (5.0, 25.0),  # modèle de secours en cas de refus
}
MODELES = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"]
EFFORTS = ["low", "medium", "high"]
# Aucune limite de longueur : chaque modèle reçoit son maximum de sortie.
# Seuls les tokens réellement générés sont facturés, ce plafond ne coûte donc rien.
MAX_SORTIE = {"claude-opus-5-5": 128000, "claude-sonnet-5-5": 128000, "claude-haiku-4-5": 64000}
CONSIGNES = {"bref": "Réponse la plus brève possible.", "detaille": "Réponse détaillée et complète."}
# Empreintes des modes livrés par les versions précédentes : un fichier identique n'a pas été
# modifié par l'utilisateur et peut être remplacé par la nouvelle version.
EMPREINTES_ANCIENNES = {
    "_base.md": {"5d8d82add8c540b6", "6a44034e2707a4f5", "ba126596960b47bd"},
    "apprendre.md": {"a6c97730b591d1c9", "5499d92e86d00f83"},
    "code.md": {"e3b8f465eb6aa9e2", "0e9fef852bafe62e"},
    "finance.md": {"63487465f7afb3d3", "f2a5aeed28064c26"},
    "general.md": {"bad4f9aaf3ad63ff", "7c823109a91a8e53"},
    "redaction.md": {"dc3b6903063cf703", "66759a92ee79ef49"},
}
MAX_TOURS_AGENT = 200     # garde-fou contre une boucle d'outils sans fin
APPROBATIONS = {}         # id -> {"event", "decision", "cid"}
ARRETS = set()            # conversations dont l'utilisateur a demandé l'arrêt
CONFIG_DEFAUT = {"modele_resume": "claude-haiku-4-5", "seuil_resume": 6000, "messages_gardes": 6,
                 "reduction_sortie": 0.0}

ID_MODE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
ID_CONV = re.compile(r"^[a-f0-9]{12}$")

PROMPT_RESUME = """Condense cet échange en notes de contexte pour poursuivre la conversation.
Garde exactement : faits établis, décisions, chiffres, noms, extraits de code ou commandes encore utiles, questions ouvertes.
Supprime tout le reste. Style télégraphique mais compréhensible. 250 mots maximum.

<resume_precedent>
{precedent}
</resume_precedent>

<echange>
{echange}
</echange>"""

app = Flask(__name__, static_folder=str(RESSOURCES / "static"), static_url_path="")
_client = None
_secours_actif = True  # désactivé automatiquement si l'API refuse le paramètre fallbacks


def client():
    """Clé saisie dans l'interface en priorité, sinon ANTHROPIC_API_KEY ou le profil `ant auth login`."""
    global _client
    if _client is None:
        cle = config().get("cle_api")
        _client = anthropic.Anthropic(api_key=cle) if cle else anthropic.Anthropic()
    return _client


def profil_anthropic():
    """Vrai si `ant auth login` a enregistré un profil (lu automatiquement par le SDK)."""
    dossier = Path(os.environ.get("ANTHROPIC_CONFIG_DIR") or Path(os.environ.get("APPDATA", "")) / "Anthropic")
    return any((dossier / "credentials").glob("*.json"))


OUTILS = RACINE / "outils"
DEPOT_ANT = "https://api.github.com/repos/anthropics/anthropic-cli/releases/latest"


def chemin_ant():
    for p in (OUTILS / "ant.exe", RACINE / "ant.exe"):
        if p.exists():
            return str(p)
    return shutil.which("ant")


def telecharger(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Concis"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def installer_ant():
    """Télécharge l'outil officiel `ant` depuis le GitHub d'Anthropic et vérifie son empreinte SHA-256."""
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else ("amd64" if sys.maxsize > 2**32 else "386")
    version = json.loads(telecharger(DEPOT_ANT))
    fichiers = {a["name"]: a["browser_download_url"] for a in version["assets"]}
    nom_zip = next((n for n in fichiers if n.endswith(f"_windows_{arch}.zip")), None)
    nom_sommes = next((n for n in fichiers if n.endswith("_checksums.txt")), None)
    if not nom_zip or not nom_sommes:
        raise RuntimeError("Aucune version Windows de l'outil de connexion n'est publiée.")
    archive = telecharger(fichiers[nom_zip])
    attendu = next((l.split()[0] for l in telecharger(fichiers[nom_sommes]).decode().splitlines()
                    if l.strip().endswith(nom_zip)), None)
    if hashlib.sha256(archive).hexdigest() != attendu:
        raise RuntimeError("Téléchargement corrompu (empreinte SHA-256 incorrecte). Réessaie.")
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        membre = next((n for n in z.namelist() if n.lower().endswith("ant.exe")), None)
        if not membre:
            raise RuntimeError("ant.exe absent de l'archive.")
        OUTILS.mkdir(parents=True, exist_ok=True)
        (OUTILS / "ant.exe").write_bytes(z.read(membre))
    return str(OUTILS / "ant.exe")


def config_publique():
    cfg = config()
    cle = cfg.pop("cle_api", "")
    if cle:
        source = "clé enregistrée"
    elif os.environ.get("ANTHROPIC_API_KEY"):
        source = "variable d'environnement"
    elif profil_anthropic():
        source = "compte Anthropic"
    else:
        source = ""
    cfg["cle_definie"], cfg["cle_source"] = bool(source), source
    cfg["ant_installe"] = bool(chemin_ant())
    return cfg


# --- Fichiers ------------------------------------------------------------------------


def lire_json(f, defaut):
    try:
        return json.loads(f.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defaut


def ecrire_json(f, data):
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(f)


def config():
    return {**CONFIG_DEFAUT, **lire_json(CONFIG, {})}


def empreinte(texte):
    return hashlib.sha256(texte.encode()).hexdigest()[:16]


def installer_modes():
    """Installe les modes livrés et met à jour ceux que l'utilisateur n'a pas modifiés."""
    if MODES.resolve() == MODES_DEFAUT.resolve():
        return
    MODES.mkdir(parents=True, exist_ok=True)
    registre_f = DONNEES / "modes_installes.json"
    registre = lire_json(registre_f, {})
    for src in MODES_DEFAUT.glob("*.md"):
        dst = MODES / src.name
        nouveau = src.read_text("utf-8")
        if dst.exists():
            actuel = empreinte(dst.read_text("utf-8"))
            connues = EMPREINTES_ANCIENNES.get(src.name, set()) | {registre.get(src.name)}
            if actuel == empreinte(nouveau) or actuel not in connues:
                continue  # déjà à jour, ou personnalisé par l'utilisateur
        dst.write_text(nouveau, "utf-8")
        registre[src.name] = empreinte(nouveau)
    ecrire_json(registre_f, registre)


def restaurer_mode(nom_fichier):
    src = MODES_DEFAUT / nom_fichier
    if not src.exists():
        return False
    texte = src.read_text("utf-8")
    (MODES / nom_fichier).write_text(texte, "utf-8")
    registre_f = DONNEES / "modes_installes.json"
    registre = lire_json(registre_f, {})
    registre[nom_fichier] = empreinte(texte)
    ecrire_json(registre_f, registre)
    return True


installer_modes()


# --- Modes (un skill par mode) -------------------------------------------------------


def lire_mode(mid):
    f = MODES / f"{mid}.md"
    if not ID_MODE.match(mid) or not f.exists():
        return None
    texte = f.read_text("utf-8")
    meta, corps = {}, texte
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", texte, re.S)
    if m:
        for ligne in m.group(1).splitlines():
            if ":" in ligne:
                k, v = ligne.split(":", 1)
                meta[k.strip()] = v.strip()
        corps = m.group(2)
    return {
        "id": mid,
        "nom": meta.get("nom", mid),
        "icone": meta.get("icone", "•"),
        "couleur": meta.get("couleur", "#d97757"),
        "modele": meta.get("modele", MODELES[0]),
        "effort": meta.get("effort", "low"),
        "ordre": int(meta.get("ordre", 99)),
        "par_defaut": (MODES_DEFAUT / f"{mid}.md").exists(),
        "description": meta.get("description", ""),
        "contenu": corps.strip(),
    }


def lister_modes():
    modes = [lire_mode(f.stem) for f in MODES.glob("*.md") if not f.stem.startswith("_")]
    return sorted((m for m in modes if m), key=lambda m: (m["ordre"], m["nom"]))


def ecrire_mode(mid, d):
    def ligne(v):
        return str(v).replace("\n", " ").strip()

    modele = d.get("modele") if d.get("modele") in MODELES else MODELES[0]
    effort = d.get("effort") if d.get("effort") in EFFORTS else "low"
    texte = (f"---\nnom: {ligne(d.get('nom') or mid)}\nicone: {ligne(d.get('icone') or '•')}\n"
             f"couleur: {ligne(d.get('couleur') or '#d97757')}\nmodele: {modele}\neffort: {effort}\n"
             f"ordre: {int(d.get('ordre') or 99)}\n"
             f"description: {ligne(d.get('description') or '')}\n---\n\n{(d.get('contenu') or '').strip()}\n")
    (MODES / f"{mid}.md").write_text(texte, "utf-8")


def prompt_systeme(mode):
    base = BASE.read_text("utf-8").strip() if BASE.exists() else ""
    return f"{base}\n\n# Mode : {mode['nom']}\n{mode['contenu']}".strip()


# --- Conversations -------------------------------------------------------------------


def chemin_conv(cid):
    if not ID_CONV.match(cid):
        abort(404)
    return CONVS / f"{cid}.json"


def charger_conv(cid):
    conv = lire_json(chemin_conv(cid), None)
    if conv is None:
        abort(404)
    return conv


def sauver_conv(conv):
    conv["maj"] = time.time()
    ecrire_json(chemin_conv(conv["id"]), conv)


def resume_conv(conv):
    d = {k: conv[k] for k in ("id", "titre", "mode", "maj", "cout", "tokens")}
    d["cout_sans"] = conv.get("cout_sans", 0.0)
    d["contexte"] = conv.get("dernier_input", 0)
    d["dossier"] = conv.get("dossier")
    d["permissions"] = conv.get("permissions")
    return d


def comptabiliser(conv, modele, u):
    pe, ps = PRIX.get(modele, PRIX["claude-opus-5-5"])
    ecrit = u.cache_creation_input_tokens or 0
    lu = u.cache_read_input_tokens or 0
    cout = (u.input_tokens * pe + ecrit * pe * 1.25 + lu * pe * 0.1 + u.output_tokens * ps) / 1e6
    conv["tokens"]["entree"] += u.input_tokens + ecrit
    conv["tokens"]["cache"] += lu
    conv["tokens"]["sortie"] += u.output_tokens
    conv["cout"] += cout
    return {"entree": u.input_tokens + ecrit, "cache": lu, "sortie": u.output_tokens,
            "cout": cout, "modele": modele}


_tokens_systeme = {}


def compter_tokens(modele, texte, systeme=None):
    kw = {"model": modele, "messages": [{"role": "user", "content": texte}]}
    if systeme:
        kw["system"] = systeme
    try:
        return client().messages.count_tokens(**kw).input_tokens
    except anthropic.APIError:
        return int(len(texte + (systeme or "")) / 3.5)  # estimation si le comptage échoue


def tokens_systeme(modele, systeme):
    """Tokens ajoutés par les règles communes + le skill (compté une fois par version)."""
    cle = (modele, systeme)
    if cle not in _tokens_systeme:
        _tokens_systeme[cle] = max(0, compter_tokens(modele, "x", systeme) - compter_tokens(modele, "x"))
    return _tokens_systeme[cle]


def bilan(conv, modele, entree, ecrit, lu, sortie, extra, tok_sys, saute=0):
    """Coût réel comparé au coût du même échange sans optimisation.

    Sans optimisation, on aurait : pas de skill, pas de cache, l'historique complet sans résumé,
    et une réponse plus longue d'un facteur 1 / (1 - reduction_sortie).
    reduction_sortie vaut 0 par défaut : seuls les gains mesurés sont alors comptés.
    """
    pe, ps = PRIX.get(modele, PRIX["claude-opus-5-5"])
    r = min(max(float(config()["reduction_sortie"]), 0.0), 0.9)
    reel_entree = (entree * pe + ecrit * pe * 1.25 + lu * pe * 0.1) / 1e6 + extra
    entree_base = max(0, entree + ecrit + lu + conv.get("economie_resume", 0) + saute - tok_sys)
    sans_entree = entree_base * pe / 1e6
    reel = reel_entree + sortie * ps / 1e6
    sans = sans_entree + sortie / (1 - r) * ps / 1e6
    return {"pct": 1 - reel / sans if sans else 0.0, "reel": reel, "sans": sans,
            "reel_entree": reel_entree, "sans_entree": sans_entree, "ps": ps, "r": r,
            "cache_lu": lu, "resume": conv.get("economie_resume", 0), "skill": tok_sys, "saute": saute,
            "sortie_evitee": round(sortie / (1 - r) - sortie)}


def resumer_si_besoin(conv, modele):
    """Remplace les anciens échanges par un résumé court quand le contexte devient lourd."""
    cfg = config()
    msgs = conv["messages"]
    if conv.get("dernier_input", 0) < int(cfg["seuil_resume"]):
        return None
    debut = len(msgs) - max(2, int(cfg["messages_gardes"]))
    while debut < len(msgs) and msgs[debut]["role"] != "user":
        debut += 1
    if debut <= conv["debut"]:
        return None
    echange = "\n\n".join(f"{'Utilisateur' if m['role'] == 'user' else 'Assistant'} : {m['contenu']}"
                          for m in msgs[conv["debut"]:debut])
    r = client().messages.create(
        model=cfg["modele_resume"], max_tokens=2000,
        messages=[{"role": "user", "content": PROMPT_RESUME.format(
            precedent=conv.get("resume") or "(aucun)", echange=echange)}])
    texte = "".join(b.text for b in r.content if b.type == "text").strip()
    if not texte:
        return None
    m = comptabiliser(conv, r.model, r.usage)
    # Le nouveau résumé remplace l'ancien résumé plus les messages résumés.
    retire = compter_tokens(modele, echange) + (compter_tokens(modele, conv["resume"]) if conv.get("resume") else 0)
    conv["economie_resume"] = max(0, conv.get("economie_resume", 0) + retire - r.usage.output_tokens)
    nb = debut - conv["debut"]
    conv["resume"], conv["debut"] = texte, debut
    return {"messages_resumes": nb, "debut": debut, "cout": m["cout"]}


def texte_envoye(m):
    """Texte tel qu'envoyé à l'API. Il doit rester identique d'un tour à l'autre pour garder le cache valide."""
    c = CONSIGNES.get(m.get("consigne"))
    return f"{m['contenu']}\n\n[{c}]" if c else m["contenu"]


def construire_messages(conv):
    """Historique en texte seul (aucun bloc de réflexion renvoyé), rôles consécutifs fusionnés."""
    dernier = conv["messages"][-1]
    if dernier.get("isole"):  # question sans historique : seul le dernier message part
        return [{"role": "user", "content": texte_envoye(dernier)}]
    msgs = []
    for m in conv["messages"][conv["debut"]:]:
        if msgs and msgs[-1]["role"] == m["role"]:
            msgs[-1]["content"] += "\n\n" + texte_envoye(m)
        else:
            msgs.append({"role": m["role"], "content": texte_envoye(m)})
    if conv.get("resume") and msgs:
        msgs[0]["content"] = (f"<contexte_precedent>\n{conv['resume']}\n</contexte_precedent>\n\n"
                              + msgs[0]["content"])
    return msgs


def ouvrir_flux(kw):
    if kw["model"] == "claude-opus-5-5" and _secours_actif:
        return client().beta.messages.stream(**kw, betas=["server-side-fallback-2026-06-01"],
                                             fallbacks=[{"model": "claude-opus-4-8"}])
    return client().messages.stream(**kw)


def sse(evenement, data):
    return f"event: {evenement}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# --- Mode projet (agent) -----------------------------------------------------------------
# L'historique API complet (réflexion et outils compris) est conservé en ajout seul :
# le cache reste valide et les blocs de réflexion restent liés à leur conversation.


def bloc_api(b):
    if b.type == "text":
        return {"type": "text", "text": b.text}
    if b.type == "thinking":
        return {"type": "thinking", "thinking": b.thinking, "signature": b.signature}
    if b.type == "redacted_thinking":
        return {"type": "redacted_thinking", "data": b.data}
    if b.type == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return None


def ajouter_user(api, blocs):
    if api and api[-1]["role"] == "user":
        api[-1]["content"].extend(blocs)
    else:
        api.append({"role": "user", "content": blocs})


def reparer_api(conv):
    """Après une interruption, chaque appel d'outil sans résultat reçoit un résultat « interrompu »."""
    api = conv.get("api") or []
    if api and api[-1]["role"] == "assistant":
        ids = [b["id"] for b in api[-1]["content"] if b["type"] == "tool_use"]
        if ids:
            api.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": i, "content": "Interrompu par l'utilisateur.", "is_error": True}
                for i in ids]})


def autorise(conv, categorie):
    p = conv.get("permissions", "demander")
    return p == "tout" or (categorie == "modif" and p == "modifs")


def executer_outil(conv, projet, bloc, etat):
    """Générateur : émet les événements d'interface et renvoie le tool_result."""
    try:
        action = projet.preparer(bloc.name, bloc.input)
    except agent.ErreurOutil as e:
        return {"type": "tool_result", "tool_use_id": bloc.id, "content": str(e), "is_error": True}

    etape = {"t": "outil", "id": action.id, "categorie": action.categorie, "titre": action.titre,
             "detail": action.detail, "statut": "en_cours"}
    if action.categorie and not autorise(conv, action.categorie):
        ev = threading.Event()
        APPROBATIONS[action.id] = {"event": ev, "decision": None, "cid": conv["id"]}
        yield sse("demande", etape)
        ev.wait(timeout=1800)
        decision = (APPROBATIONS.pop(action.id, None) or {}).get("decision") or "non"
        if decision == "toujours":
            conv["permissions"] = "tout" if action.categorie == "commande" else "modifs"
            yield sse("permissions", {"permissions": conv["permissions"]})
        if decision == "non":
            etape["statut"] = "refuse"
            etat["blocs"].append(etape)
            yield sse("etape", etape)
            return {"type": "tool_result", "tool_use_id": bloc.id,
                    "content": "L'utilisateur a refusé cette action.", "is_error": True}

    try:
        resultat = action.executer()
        etape["statut"] = "ok"
    except agent.ErreurOutil as e:
        resultat, etape["statut"] = str(e), "erreur"
    except OSError as e:
        resultat, etape["statut"] = f"Erreur système : {e}", "erreur"
    if action.categorie == "commande" or etape["statut"] == "erreur":
        etape["detail"] = resultat[-6000:]
    if action.categorie == "modif" and etape["statut"] == "ok":
        etape.update(fichier=action.fichier, sauvegarde=action.sauvegarde, existait=action.existait)
    etat["blocs"].append(etape)
    yield sse("etape", etape)
    return {"type": "tool_result", "tool_use_id": bloc.id, "content": resultat,
            "is_error": etape["statut"] == "erreur"}


def flux_agent(conv, mode, etat):
    cid = conv["id"]
    projet = agent.Projet(conv["dossier"], DONNEES / "sauvegardes" / cid)
    systeme = prompt_systeme(mode) + agent.CONSIGNES.format(dossier=conv["dossier"])
    tok_sys = tokens_systeme(mode["modele"], systeme)
    api = conv.setdefault("api", [])
    texte = texte_envoye(conv["messages"][-1])
    notes = conv.pop("notes_agent", [])
    if notes:
        texte = "\n".join(notes) + "\n\n" + texte
    ajouter_user(api, [{"type": "text", "text": texte}])

    total = {"reel": 0.0, "sans": 0.0, "cache_lu": 0, "sortie_evitee": 0, "entree": 0, "cache": 0,
             "sortie": 0, "cout": 0.0}
    modele = mode["modele"]
    for _ in range(MAX_TOURS_AGENT):
        if cid in ARRETS:
            break
        kw = {"model": mode["modele"], "max_tokens": MAX_SORTIE.get(mode["modele"], 64000),
              "system": systeme, "messages": api, "tools": agent.OUTILS,
              "cache_control": {"type": "ephemeral"},
              # Efface côté serveur les anciens résultats d'outils quand le contexte grossit.
              "betas": ["context-management-2025-06-27"],
              "context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]}}
        if not mode["modele"].startswith("claude-haiku"):
            kw["output_config"] = {"effort": mode["effort"]}

        with client().beta.messages.stream(**kw) as stream:
            for ev in stream:
                if ev.type == "content_block_delta" and ev.delta.type == "text_delta":
                    if not etat["blocs"] or etat["blocs"][-1]["t"] != "texte":
                        etat["blocs"].append({"t": "texte", "v": ""})
                        if etat["morceaux"]:
                            etat["morceaux"].append("\n\n")
                    etat["blocs"][-1]["v"] += ev.delta.text
                    etat["morceaux"].append(ev.delta.text)
                    yield sse("delta", {"t": ev.delta.text})
            final = stream.get_final_message()

        u = final.usage
        modele = final.model
        m = comptabiliser(conv, modele, u)
        b = bilan(conv, modele, u.input_tokens, u.cache_creation_input_tokens or 0,
                  u.cache_read_input_tokens or 0, u.output_tokens, 0.0, tok_sys)
        for k in ("entree", "cache", "sortie", "cout"):
            total[k] += m[k]
        for k in ("reel", "sans", "cache_lu", "sortie_evitee"):
            total[k] += b[k]
        conv["cout_sans"] = conv.get("cout_sans", 0.0) + b["sans"]
        conv["dernier_input"] = u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0)

        contenu = [x for x in (bloc_api(blk) for blk in final.content) if x]
        if contenu:
            api.append({"role": "assistant", "content": contenu})
        if final.stop_reason != "tool_use":
            note = {"refusal": "\n\n> Claude a refusé de poursuivre cette demande.",
                    "max_tokens": "\n\n> Réponse coupée : longueur maximale atteinte. Écris « continue »."
                    }.get(final.stop_reason)
            if note:
                etat["morceaux"].append(note)
                etat["blocs"].append({"t": "texte", "v": note})
            break

        resultats = []
        for blk in final.content:
            if blk.type == "tool_use":
                resultats.append((yield from executer_outil(conv, projet, blk, etat)))
        ajouter_user(api, resultats)
        sauver_conv(conv)

    eco = {"pct": 1 - total["reel"] / total["sans"] if total["sans"] else 0.0, "reel": total["reel"],
           "sans": total["sans"], "cache_lu": total["cache_lu"], "resume": 0, "saute": 0,
           "skill": tok_sys, "sortie_evitee": total["sortie_evitee"],
           "r": min(max(float(config()["reduction_sortie"]), 0.0), 0.9)}
    etat["meta"] = {"entree": total["entree"], "cache": total["cache"], "sortie": total["sortie"],
                    "cout": total["cout"], "modele": modele, "eco": eco}
    yield sse("fin", {"meta": etat["meta"], "texte": "".join(etat["morceaux"]), "blocs": etat["blocs"],
                      "conv": resume_conv(conv)})


# --- Routes ------------------------------------------------------------------------------


@app.get("/")
def accueil():
    return app.send_static_file("index.html")


@app.get("/api/modes")
def api_modes():
    return jsonify(lister_modes())


@app.put("/api/modes/<mid>")
def api_ecrire_mode(mid):
    if not ID_MODE.match(mid):
        return jsonify(erreur="Identifiant invalide : minuscules, chiffres, - et _ (32 max)."), 400
    ecrire_mode(mid, request.get_json(force=True))
    return jsonify(lire_mode(mid))


@app.delete("/api/modes/<mid>")
def api_supprimer_mode(mid):
    if mid == "general":
        return jsonify(erreur="Le mode Général ne peut pas être supprimé."), 400
    f = MODES / f"{mid}.md"
    if ID_MODE.match(mid) and f.exists():
        f.unlink()
    return jsonify(ok=True)


@app.post("/api/modes/<mid>/restaurer")
def api_restaurer_mode(mid):
    nom = "_base.md" if mid == "_base" else f"{mid}.md"
    if not (mid == "_base" or ID_MODE.match(mid)) or not restaurer_mode(nom):
        return jsonify(erreur="Aucune version par défaut pour ce mode."), 404
    return jsonify(ok=True)


@app.get("/api/base")
def api_base():
    return jsonify(contenu=BASE.read_text("utf-8") if BASE.exists() else "")


@app.put("/api/base")
def api_ecrire_base():
    BASE.write_text(request.get_json(force=True).get("contenu", "").strip() + "\n", "utf-8")
    return jsonify(ok=True)


@app.get("/api/config")
def api_config():
    return jsonify(config=config_publique(), modeles=MODELES)


@app.put("/api/config")
def api_ecrire_config():
    d = request.get_json(force=True)
    cfg = config()
    if d.get("modele_resume") in MODELES:
        cfg["modele_resume"] = d["modele_resume"]
    cfg["seuil_resume"] = min(max(int(d.get("seuil_resume", cfg["seuil_resume"])), 1000), 200000)
    cfg["messages_gardes"] = min(max(int(d.get("messages_gardes", cfg["messages_gardes"])), 2), 40)
    cfg["reduction_sortie"] = min(max(float(d.get("reduction_sortie", cfg["reduction_sortie"])), 0.0), 0.9)
    if d.get("effacer_cle"):
        cfg.pop("cle_api", None)
    elif str(d.get("cle_api", "")).strip():
        cfg["cle_api"] = str(d["cle_api"]).strip()
    ecrire_json(CONFIG, cfg)
    global _client
    _client = None  # recréé avec la nouvelle clé
    return jsonify(config=config_publique())


@app.get("/api/conversations")
def api_conversations():
    convs = [lire_json(f, None) for f in CONVS.glob("*.json")]
    convs = sorted((c for c in convs if c), key=lambda c: c["maj"], reverse=True)
    return jsonify([resume_conv(c) for c in convs])


PERMISSIONS = ["demander", "modifs", "tout"]


def dossier_valide(chemin):
    if not chemin:
        return None
    p = Path(str(chemin).strip().strip('"')).expanduser()
    return str(p.resolve()) if p.is_dir() else None


@app.post("/api/conversations")
def api_nouvelle_conversation():
    d = request.get_json(force=True) or {}
    mode = d.get("mode", "general")
    t = time.time()
    conv = {"id": uuid.uuid4().hex[:12], "titre": "", "mode": mode if lire_mode(mode) else "general",
            "cree": t, "maj": t, "messages": [], "resume": "", "debut": 0, "dernier_input": 0,
            "tokens": {"entree": 0, "sortie": 0, "cache": 0}, "cout": 0.0}
    dossier = dossier_valide(d.get("dossier"))
    if conv["mode"] == "code" and dossier:
        conv["dossier"] = dossier
        conv["permissions"] = d.get("permissions") if d.get("permissions") in PERMISSIONS else "demander"
    sauver_conv(conv)
    return jsonify(conv)


@app.post("/api/dossier")
def api_verifier_dossier():
    chemin = dossier_valide((request.get_json(force=True) or {}).get("chemin"))
    if not chemin:
        return jsonify(erreur="Dossier introuvable."), 400
    return jsonify(chemin=chemin)


@app.post("/api/ouvrir")
def api_ouvrir_dossier():
    chemin = dossier_valide((request.get_json(force=True) or {}).get("chemin"))
    if chemin and hasattr(os, "startfile"):
        os.startfile(chemin)
    return jsonify(ok=bool(chemin))


@app.post("/api/connexion")
def api_connexion():
    """Lance `ant auth login` : connexion au compte Anthropic dans le navigateur, sans clé à copier."""
    ant = chemin_ant()
    try:
        if not ant:
            ant = installer_ant()
    except (OSError, ValueError, RuntimeError, KeyError) as e:
        return jsonify(erreur=f"Installation de l'outil de connexion impossible : {e}"), 502
    journal_f = DONNEES / "connexion.log"
    journal_f.parent.mkdir(parents=True, exist_ok=True)
    with open(journal_f, "w", encoding="utf-8") as journal:
        p = subprocess.Popen([ant, "auth", "login"], stdout=journal, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        code = p.wait(timeout=4)  # un échec immédiat (option inconnue, erreur réseau…) est remonté
    except subprocess.TimeoutExpired:
        return jsonify(ok=True)   # en attente de la connexion dans le navigateur
    if code != 0 and not profil_anthropic():
        return jsonify(erreur="La connexion a échoué : " + journal_f.read_text("utf-8", errors="replace")[-400:]), 502
    return jsonify(ok=True)


@app.post("/api/connexion/verifier")
def api_verifier_connexion():
    global _client
    _client = None  # le prochain appel relit les identifiants (profil fraîchement créé)
    return jsonify(config=config_publique())


@app.post("/api/approbations/<aid>")
def api_approuver(aid):
    a = APPROBATIONS.get(aid)
    if not a:
        return jsonify(erreur="Demande expirée."), 404
    decision = (request.get_json(force=True) or {}).get("decision")
    a["decision"] = decision if decision in ("oui", "non", "toujours") else "non"
    a["event"].set()
    return jsonify(ok=True)


@app.post("/api/conversations/<cid>/arreter")
def api_arreter(cid):
    ARRETS.add(cid)
    for a in list(APPROBATIONS.values()):
        if a["cid"] == cid:
            a["decision"] = "non"
            a["event"].set()
    return jsonify(ok=True)


@app.post("/api/conversations/<cid>/annuler/<eid>")
def api_annuler_etape(cid, eid):
    conv = charger_conv(cid)
    for m in conv["messages"]:
        for b in m.get("blocs", []):
            if b.get("id") == eid and b.get("categorie") == "modif" and not b.get("annule"):
                try:
                    agent.annuler(b, conv["dossier"])
                except agent.ErreurOutil as e:
                    return jsonify(erreur=str(e)), 400
                b["annule"] = True
                # Claude doit savoir que le fichier a changé : la note est ajoutée à son prochain message.
                conv.setdefault("notes_agent", []).append(f"L'utilisateur a annulé : {b['titre']}.")
                sauver_conv(conv)
                return jsonify(ok=True)
    return jsonify(erreur="Modification introuvable ou déjà annulée."), 404


@app.get("/api/conversations/<cid>")
def api_conversation(cid):
    return jsonify(charger_conv(cid))


@app.patch("/api/conversations/<cid>")
def api_modifier_conversation(cid):
    conv = charger_conv(cid)
    d = request.get_json(force=True)
    if "titre" in d:
        conv["titre"] = str(d["titre"]).strip()[:80]
    if "mode" in d and lire_mode(d["mode"]) and not conv.get("dossier"):
        conv["mode"] = d["mode"]
    if d.get("permissions") in PERMISSIONS and conv.get("dossier"):
        conv["permissions"] = d["permissions"]
    sauver_conv(conv)
    return jsonify(resume_conv(conv))


@app.delete("/api/conversations/<cid>")
def api_supprimer_conversation(cid):
    chemin_conv(cid).unlink(missing_ok=True)
    return jsonify(ok=True)


@app.post("/api/conversations/<cid>/messages")
def api_envoyer(cid):
    conv = charger_conv(cid)
    d = request.get_json(force=True) or {}
    # Espaces superflus retirés : ils seraient facturés à chaque renvoi de l'historique.
    texte = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", d.get("texte", ""))).strip()
    if not texte:
        return jsonify(erreur="Message vide."), 400
    mode = lire_mode(conv["mode"]) or lire_mode("general")
    est_agent = bool(conv.get("dossier"))
    isole = not est_agent and bool(d.get("isole")) and any(m["role"] == "assistant" for m in conv["messages"])
    message = {"role": "user", "contenu": texte, "t": time.time()}
    if d.get("consigne") in CONSIGNES:
        message["consigne"] = d["consigne"]
    if isole:
        message["isole"] = True
    conv["messages"].append(message)
    if not conv["titre"]:
        conv["titre"] = texte.splitlines()[0][:60]
    sauver_conv(conv)

    ARRETS.discard(cid)

    def flux():
        global _secours_actif
        etat = {"morceaux": [], "blocs": [], "meta": None}
        morceaux = etat["morceaux"]
        try:
            if est_agent:
                yield from flux_agent(conv, mode, etat)
                return
            extra = 0.0  # coût d'un éventuel résumé, imputé à ce message
            r = None if isole else resumer_si_besoin(conv, mode["modele"])
            if r:
                extra = r["cout"]
                sauver_conv(conv)
                yield sse("resume", r)

            systeme = prompt_systeme(mode)
            tok_sys = tokens_systeme(mode["modele"], systeme)
            # Question sans historique : le contexte habituel n'est pas envoyé.
            saute = max(0, conv.get("dernier_input", 0) - tok_sys) if isole else 0
            kw = {"model": mode["modele"], "max_tokens": MAX_SORTIE.get(mode["modele"], 64000),
                  "system": systeme, "messages": construire_messages(conv),
                  "cache_control": {"type": "ephemeral"}}
            if not mode["modele"].startswith("claude-haiku"):
                kw["output_config"] = {"effort": mode["effort"]}

            for essai in range(2):
                try:
                    with ouvrir_flux(kw) as stream:
                        for ev in stream:
                            if ev.type == "message_start":
                                u = ev.message.usage
                                yield sse("debut", bilan(conv, ev.message.model, u.input_tokens,
                                                         u.cache_creation_input_tokens or 0,
                                                         u.cache_read_input_tokens or 0, 0, extra, tok_sys, saute))
                            elif ev.type == "content_block_delta" and ev.delta.type == "text_delta":
                                morceaux.append(ev.delta.text)
                                yield sse("delta", {"t": ev.delta.text})
                        final = stream.get_final_message()
                    break
                except anthropic.BadRequestError as e:
                    if essai == 0 and _secours_actif and not morceaux and "fallback" in str(e).lower():
                        _secours_actif = False
                        continue
                    raise

            u = final.usage
            if not isole:  # le contexte d'une question isolée ne reflète pas la conversation
                conv["dernier_input"] = (u.input_tokens + (u.cache_creation_input_tokens or 0)
                                         + (u.cache_read_input_tokens or 0))
            meta = etat["meta"] = comptabiliser(conv, final.model, u)
            meta["eco"] = bilan(conv, final.model, u.input_tokens, u.cache_creation_input_tokens or 0,
                                u.cache_read_input_tokens or 0, u.output_tokens, extra, tok_sys, saute)
            conv["cout_sans"] = conv.get("cout_sans", 0.0) + meta["eco"]["sans"]
            if final.stop_reason == "refusal":
                morceaux.append("\n\n> Claude a refusé de répondre à cette demande.")
            elif final.stop_reason == "max_tokens":
                morceaux.append("\n\n> Réponse coupée : longueur maximale du modèle atteinte. Écris « continue ».")
            yield sse("fin", {"meta": meta, "texte": "".join(morceaux), "conv": resume_conv(conv)})
        except anthropic.AuthenticationError:
            yield sse("erreur", {"message": "Clé API invalide ou révoquée.", "code": "cle"})
        except TypeError:  # le SDK n'a trouvé aucune clé
            yield sse("erreur", {"message": "Aucune clé API configurée.", "code": "cle"})
        except anthropic.RateLimitError:
            yield sse("erreur", {"message": "Limite de débit atteinte. Réessaie dans quelques secondes."})
        except anthropic.APIConnectionError:
            yield sse("erreur", {"message": "Connexion à l'API impossible. Vérifie ta connexion internet."})
        except anthropic.APIStatusError as e:
            yield sse("erreur", {"message": f"Erreur API {e.status_code} : {e.message}"})
        finally:
            if morceaux or etat["blocs"]:
                message = {"role": "assistant", "contenu": "".join(morceaux), "t": time.time(), "meta": etat["meta"]}
                if est_agent:
                    message["blocs"] = etat["blocs"]
                conv["messages"].append(message)
            if est_agent:
                reparer_api(conv)
            ARRETS.discard(cid)
            sauver_conv(conv)

    return Response(flux(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def port_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def attendre_serveur(port, delai=15):
    fin = time.time() + delai
    while time.time() < fin:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


if __name__ == "__main__":
    # En mode fenêtré (sans console), stdout/stderr valent None : Flask planterait en écrivant ses logs.
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    CONVS.mkdir(parents=True, exist_ok=True)

    port = int(os.environ.get("CONCIS_PORT") or port_libre())
    url = f"http://127.0.0.1:{port}"
    serveur = threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port, threaded=True,
                                                      use_reloader=False), daemon=True)
    serveur.start()
    attendre_serveur(port)

    if os.environ.get("CONCIS_SANS_FENETRE"):  # mode test : serveur seul
        print(url, flush=True)
        serveur.join()
    else:
        try:
            import webview
        except ImportError:  # repli : navigateur par défaut
            webbrowser.open(url)
            serveur.join()
        else:
            class ApiFenetre:
                """Fonctions natives appelables depuis l'interface (window.pywebview.api)."""

                def choisir_dossier(self):
                    r = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER)
                    return r[0] if r else ""

            webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
            webview.create_window("Concis", url, width=1280, height=840, min_size=(720, 520), js_api=ApiFenetre())
            webview.start()  # bloque jusqu'à la fermeture de la fenêtre ; le serveur s'arrête avec
