"""
Mode projet : Claude lit, cherche et modifie des fichiers et lance des commandes dans un dossier choisi.

Sécurité :
  - chaque chemin est résolu puis confiné au dossier du projet ;
  - les modifications et les commandes passent par la validation de l'utilisateur (sauf permission accordée) ;
  - chaque fichier modifié est sauvegardé avant modification, ce qui permet d'annuler.

Économie de tokens :
  - recherche par motif plutôt que lecture de fichiers entiers ;
  - lecture partielle (view_range) et troncature des sorties trop longues ;
  - arborescence compacte qui ignore les dossiers volumineux (.git, node_modules…).
"""

import difflib
import fnmatch
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

IGNORES = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build", ".idea",
           ".vscode", ".next", "target", ".mypy_cache", ".pytest_cache", "coverage"}
MAX_RESULTAT = 12000      # caractères renvoyés à Claude par résultat d'outil
MAX_LIGNES_SANS_PLAGE = 400
DELAI_COMMANDE = 180      # secondes

OUTILS = [
    {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool", "max_characters": MAX_RESULTAT},
    {
        "name": "rechercher",
        "description": "Cherche un motif (expression régulière) dans les fichiers texte du projet. "
                       "Renvoie « chemin:ligne: texte ». À utiliser avant de lire un fichier pour cibler les lignes utiles.",
        "input_schema": {
            "type": "object",
            "properties": {
                "motif": {"type": "string", "description": "Expression régulière Python"},
                "fichiers": {"type": "string", "description": "Filtre glob facultatif, ex. *.py"},
            },
            "required": ["motif"],
        },
    },
    {
        "name": "powershell",
        "description": "Exécute une commande PowerShell dans le dossier du projet (Windows) et renvoie la sortie "
                       "et le code de retour. Pour lancer, tester, installer, utiliser git.",
        "input_schema": {
            "type": "object",
            "properties": {"commande": {"type": "string"}},
            "required": ["commande"],
        },
    },
]

CONSIGNES = """

# Accès direct au projet
Tu travailles dans le dossier « {dossier} » (Windows), avec des outils. Chemins relatifs à ce dossier.
- Explore le minimum : `rechercher` pour localiser, puis `view` avec `view_range` pour lire seulement les lignes utiles. `view` sur « . » donne l'arborescence.
- Modifie avec des `str_replace` ciblés. `create` seulement pour un nouveau fichier.
- `powershell` pour lancer, tester ou installer. Vérifie ton travail en l'exécutant quand c'est possible.
- Enchaîne les outils sans commentaire entre les appels. Ne relis pas un fichier que tu viens de modifier.
- Si l'utilisateur refuse une action, ne la retente pas : adapte-toi ou demande-lui.
- À la fin : 1 à 3 lignes (ce qui a été fait, ce qui reste à faire)."""


class ErreurOutil(Exception):
    pass


def tronquer(texte, limite=MAX_RESULTAT):
    if len(texte) <= limite:
        return texte
    tete = limite // 4
    return f"{texte[:tete]}\n[… {len(texte) - limite} caractères omis …]\n{texte[-(limite - tete):]}"


def diff(avant, apres, nom):
    lignes = list(difflib.unified_diff(avant.splitlines(), apres.splitlines(), f"a/{nom}", f"b/{nom}",
                                       lineterm="", n=2))
    if len(lignes) > 300:
        lignes = lignes[:300] + [f"… {len(lignes) - 300} lignes de diff omises"]
    return "\n".join(lignes)


class Action:
    """Action en attente : `categorie` vaut None (lecture), « modif » ou « commande »."""

    def __init__(self, categorie, titre, detail, executer, fichier=None):
        self.id = uuid.uuid4().hex[:10]
        self.categorie, self.titre, self.detail = categorie, titre, detail
        self.executer, self.fichier = executer, fichier
        self.sauvegarde = None
        self.existait = True


class Projet:
    def __init__(self, racine, dossier_sauvegardes):
        self.racine = Path(racine).resolve()
        self.sauvegardes = Path(dossier_sauvegardes)

    # --- Chemins ---

    def resoudre(self, chemin):
        brut = str(chemin or ".").strip()
        p = Path(brut)
        if not p.is_absolute():
            p = self.racine / brut.lstrip("/\\")
        p = p.resolve()
        if not p.is_relative_to(self.racine):
            raise ErreurOutil(f"Chemin hors du dossier du projet : {brut}")
        return p

    def relatif(self, p):
        r = p.relative_to(self.racine).as_posix()
        return r or "."

    def fichiers(self, filtre=None):
        for base, dossiers, noms in os.walk(self.racine):
            dossiers[:] = sorted(d for d in dossiers if d not in IGNORES and not d.startswith("."))
            for n in sorted(noms):
                if filtre and not fnmatch.fnmatch(n, filtre):
                    continue
                yield Path(base) / n

    # --- Préparation des actions ---

    def preparer(self, nom, entree):
        if nom == "rechercher":
            return Action(None, f"Recherche « {entree.get('motif', '')} »", "",
                          lambda: self.rechercher(entree.get("motif", ""), entree.get("fichiers")))
        if nom == "powershell":
            cmd = entree.get("commande", "")
            return Action("commande", cmd, "", lambda: self.powershell(cmd))
        if nom == "str_replace_based_edit_tool":
            return self.preparer_editeur(entree)
        raise ErreurOutil(f"Outil inconnu : {nom}")

    def preparer_editeur(self, e):
        cmd = e.get("command")
        p = self.resoudre(e.get("path"))
        rel = self.relatif(p)

        if cmd == "view":
            plage = e.get("view_range")
            suffixe = f" (lignes {plage[0]}–{plage[1]})" if plage else ""
            return Action(None, f"Lecture {rel}{suffixe}", "", lambda: self.voir(p, plage))

        if cmd == "create":
            nouveau = e.get("file_text", "")
            avant = self.lire(p) if p.exists() else ""
            a = Action("modif", f"{'Réécriture' if p.exists() else 'Création'} {rel}",
                       diff(avant, nouveau, rel), None, rel)
            a.existait = p.exists()
            a.executer = lambda: self.ecrire(a, p, nouveau, "Fichier créé." if not a.existait else "Fichier réécrit.")
            return a

        if cmd == "str_replace":
            avant = self.lire(p)
            ancien, neuf = e.get("old_str", ""), e.get("new_str", "")
            n = avant.count(ancien) if ancien else 0
            if n == 0:
                raise ErreurOutil("Aucune correspondance exacte pour old_str. Relis les lignes concernées.")
            if n > 1:
                raise ErreurOutil(f"{n} correspondances pour old_str : ajoute du contexte pour la rendre unique.")
            apres = avant.replace(ancien, neuf, 1)
            a = Action("modif", f"Modification {rel}", diff(avant, apres, rel), None, rel)
            a.executer = lambda: self.ecrire(a, p, apres, "Modification appliquée.")
            return a

        if cmd == "insert":
            avant = self.lire(p)
            lignes = avant.split("\n")
            n = int(e.get("insert_line", 0))
            if not 0 <= n <= len(lignes):
                raise ErreurOutil(f"insert_line hors limites (0 à {len(lignes)}).")
            texte = e.get("insert_text", e.get("new_str", ""))
            apres = "\n".join(lignes[:n] + texte.split("\n") + lignes[n:])
            a = Action("modif", f"Insertion {rel} (ligne {n})", diff(avant, apres, rel), None, rel)
            a.executer = lambda: self.ecrire(a, p, apres, "Texte inséré.")
            return a

        raise ErreurOutil(f"Commande d'éditeur non prise en charge : {cmd}")

    # --- Exécution ---

    def lire(self, p):
        if not p.is_file():
            raise ErreurOutil(f"Fichier introuvable : {self.relatif(p)}")
        return p.read_text("utf-8", errors="replace")

    def voir(self, p, plage):
        if p.is_dir():
            lignes = []
            for base, dossiers, noms in os.walk(p):
                profondeur = len(Path(base).relative_to(p).parts)
                dossiers[:] = sorted(d for d in dossiers if d not in IGNORES and not d.startswith("."))
                if profondeur >= 2:
                    dossiers[:] = []
                retrait = "  " * profondeur
                lignes += [f"{retrait}{d}/" for d in dossiers]
                lignes += [f"{retrait}{n}" for n in sorted(noms)]
                if len(lignes) > 300:
                    lignes.append("[… liste tronquée]")
                    break
            return "\n".join(lignes) or "(dossier vide)"
        lignes = self.lire(p).split("\n")
        debut, fin = 1, len(lignes)
        note = ""
        if plage:
            debut = max(1, int(plage[0]))
            fin = len(lignes) if int(plage[1]) == -1 else min(len(lignes), int(plage[1]))
        elif len(lignes) > MAX_LIGNES_SANS_PLAGE:
            fin = MAX_LIGNES_SANS_PLAGE
            note = f"\n[Fichier de {len(lignes)} lignes : utilise view_range pour la suite.]"
        corps = "\n".join(f"{i:>5}\t{lignes[i - 1]}" for i in range(debut, fin + 1))
        return tronquer(corps) + note

    def ecrire(self, action, p, contenu, message):
        crlf = False
        if p.exists():
            crlf = b"\r\n" in p.read_bytes()  # conserve les fins de ligne Windows du fichier
            self.sauvegardes.mkdir(parents=True, exist_ok=True)
            action.sauvegarde = str(self.sauvegardes / f"{action.id}_{p.name}")
            shutil.copy2(p, action.sauvegarde)
        contenu = contenu.replace("\r\n", "\n")
        if crlf:
            contenu = contenu.replace("\n", "\r\n")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(contenu, "utf-8", newline="")
        return message

    def rechercher(self, motif, filtre):
        try:
            rx = re.compile(motif)
        except re.error as e:
            raise ErreurOutil(f"Expression régulière invalide : {e}")
        resultats = []
        for f in self.fichiers(filtre):
            try:
                if f.stat().st_size > 1_000_000:
                    continue
                texte = f.read_text("utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for i, ligne in enumerate(texte.split("\n"), 1):
                if rx.search(ligne):
                    resultats.append(f"{self.relatif(f)}:{i}: {ligne.strip()[:200]}")
                    if len(resultats) >= 80:
                        return "\n".join(resultats) + "\n[… 80 premiers résultats : affine le motif]"
        return "\n".join(resultats) or "Aucun résultat."

    def powershell(self, commande):
        prefixe = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", prefixe + commande],
                cwd=self.racine, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=DELAI_COMMANDE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise ErreurOutil(f"Commande interrompue après {DELAI_COMMANDE} s.")
        sortie = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()
        return tronquer(f"{sortie}\n[code de retour : {r.returncode}]".strip())


def annuler(etape, racine):
    """Remet un fichier dans l'état précédant une modification."""
    p = (Path(racine) / etape["fichier"]).resolve()
    if not p.is_relative_to(Path(racine).resolve()):
        raise ErreurOutil("Chemin invalide.")
    if etape.get("sauvegarde") and Path(etape["sauvegarde"]).exists():
        shutil.copy2(etape["sauvegarde"], p)
    elif not etape.get("existait", True) and p.exists():
        p.unlink()
    else:
        raise ErreurOutil("Aucune sauvegarde pour cette modification.")
