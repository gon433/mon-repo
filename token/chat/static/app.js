"use strict";

const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};

const NOMS_MODELES = {
  "claude-opus-5-5": "Opus 5.5",
  "claude-sonnet-5-5": "Sonnet 5.5",
  "claude-haiku-4-5": "Haiku 4.5",
  "claude-opus-4-8": "Opus 4.8",
};
const fmtUSD = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4 });
const fmtTok = (n) => (n >= 1000 ? (n / 1000).toFixed(1).replace(".", ",") + " k" : String(n || 0));
const estimer = (txt) => Math.ceil((txt || "").length / 3.5);

const stock = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* stockage indisponible */ } },
};

const etat = {
  modes: [], convs: [], conv: null, modeId: "general", ctrl: null,
  base: "",                              // règles communes (pour l'estimation des tokens)
  contexte: 0,                           // tokens d'historique renvoyés au prochain message
  longueur: stock.get("longueur") || "", // "" (auto), "bref" ou "detaille"
  isole: false,                          // prochaine question envoyée sans historique
  dossier: stock.get("dossier") || "",   // dossier de projet pour la prochaine conversation Code
  permissions: stock.get("permissions") || "demander",
  config: {},
};
const SEUIL_CONTEXTE_LONG = 12000;
const LIBELLES = { bref: "Bref", detaille: "Détaillé" };

// --- Estimation du coût avant envoi ----------------------------------------------

function tokensInstructions() {
  const m = modeCourant();
  return estimer(etat.base) + estimer(m ? m.contenu : "");
}

function majCout() {
  const msg = estimer($("#input").value);
  const sansHisto = etat.isole || !etat.conv;
  const total = (sansHisto ? tokensInstructions() : Math.max(etat.contexte, tokensInstructions())) + msg;
  const c = $("#cost");
  c.textContent = `≈ ${fmtTok(total)} tokens`;
  c.classList.toggle("lourd", total > SEUIL_CONTEXTE_LONG);

  const banner = $("#banner");
  const long = etat.conv && !etat.isole && etat.contexte > SEUIL_CONTEXTE_LONG;
  banner.hidden = !long;
  if (long) {
    const nouv = el("button", null, "Nouvelle conversation");
    nouv.type = "button";
    nouv.onclick = () => { const t = $("#input").value; nouvelle(); $("#input").value = t; ajuster(); };
    const iso = el("button", null, "Question sans historique");
    iso.type = "button";
    iso.onclick = () => basculerIsole(true);
    banner.replaceChildren(
      el("span", null, `Conversation longue : chaque message renvoie ~${fmtTok(etat.contexte)} tokens d'historique. Nouveau sujet ?`),
      nouv, iso);
  }
}

function majLongueur() {
  document.querySelectorAll("#longueur button").forEach((b) => b.classList.toggle("on", b.dataset.v === etat.longueur));
}

function basculerIsole(valeur) {
  etat.isole = typeof valeur === "boolean" ? valeur : !etat.isole;
  $("#isole").classList.toggle("on", etat.isole);
  majCout();
  $("#input").focus();
}

async function api(url, opts = {}) {
  const r = await fetch(url, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.erreur || r.statusText);
  return data;
}

// --- Thème ---------------------------------------------------------------

function appliquerTheme(t) {
  if (t) document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  const sombre = t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  $("#hl-theme").href = `https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/${sombre ? "github-dark" : "github"}.min.css`;
}

function basculerTheme() {
  const actuel = document.documentElement.dataset.theme
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const suivant = actuel === "dark" ? "light" : "dark";
  stock.set("theme", suivant);
  appliquerTheme(suivant);
}

// --- Markdown --------------------------------------------------------------------

marked.use({ gfm: true, breaks: false });
const rendre = (md) => DOMPurify.sanitize(marked.parse(md || ""));

function copier(texte, bouton) {
  navigator.clipboard?.writeText(texte).then(() => {
    const avant = bouton.textContent;
    bouton.textContent = "Copié ✓";
    setTimeout(() => (bouton.textContent = avant), 1200);
  });
}

function decorer(racine) {
  // Liens externes : ouverts dans le navigateur, pas dans la fenêtre de l'application.
  racine.querySelectorAll("a[href^='http']").forEach((a) => { a.target = "_blank"; a.rel = "noopener"; });
  racine.querySelectorAll("pre code").forEach((code) => {
    if (!code.dataset.hl) {
      try { hljs.highlightElement(code); } catch { /* langage inconnu */ }
      code.dataset.hl = "1";
    }
    const pre = code.parentElement;
    if (!pre.querySelector(".copy")) {
      const b = el("button", "copy", "Copier");
      b.type = "button";
      b.onclick = () => copier(code.innerText, b);
      pre.appendChild(b);
    }
  });
}

// --- Modes --------------------------------------------------------------------------

const modeDe = (id) => etat.modes.find((m) => m.id === id) || etat.modes[0];
const modeCourant = () => modeDe(etat.conv ? etat.conv.mode : etat.modeId);

async function chargerModes() {
  etat.modes = await api("/api/modes");
  if (!etat.modes.some((m) => m.id === etat.modeId) && etat.modes[0]) etat.modeId = etat.modes[0].id;
}

function afficherModeChip() {
  const m = modeCourant();
  if (!m) return;
  const b = $("#mode-btn");
  b.style.setProperty("--c", m.couleur);
  b.replaceChildren(el("span", "ic", m.icone), el("span", null, m.nom), el("span", null, "▾"));
  document.documentElement.style.setProperty("--accent", m.couleur);
  const verrou = !!(etat.conv && etat.conv.dossier);
  b.classList.toggle("verrou", verrou);
  b.title = verrou ? "Conversation projet : le mode reste Code" : "Changer de mode";
  afficherProjet();
  majCout();
}

function basculerMenuModes() {
  if (etat.conv && etat.conv.dossier) return;
  const menu = $("#mode-menu");
  const cur = modeCourant();
  menu.replaceChildren();
  etat.modes.forEach((m) => {
    const o = el("button", "mode-opt" + (m.id === cur.id ? " active" : ""));
    o.type = "button";
    o.style.setProperty("--c", m.couleur);
    const t = el("div");
    t.append(el("div", "n", m.nom), el("div", "d", m.description));
    o.append(el("span", "ic", m.icone), t);
    o.onclick = () => { menu.hidden = true; choisirMode(m.id); };
    menu.append(o);
  });
  menu.hidden = !menu.hidden;
}

async function choisirMode(id) {
  etat.modeId = id;
  if (etat.conv && etat.conv.mode !== id) {
    etat.conv.mode = id;
    try {
      await api(`/api/conversations/${etat.conv.id}`, { method: "PATCH", body: { mode: id } });
      await chargerConvs();
    } catch (e) { erreur(e.message); }
  }
  afficherModeChip();
  if (!etat.conv) afficherAccueil();
  $("#input").focus();
}

// --- Conversations ---------------------------------------------------------------

async function chargerConvs() {
  etat.convs = await api("/api/conversations");
  afficherConvs();
}

function groupe(ts) {
  const now = new Date();
  const jour = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  if (ts >= jour) return "Aujourd'hui";
  if (ts >= jour - 86400) return "Hier";
  if (ts >= jour - 6 * 86400) return "7 derniers jours";
  return "Plus ancien";
}

function afficherConvs() {
  const liste = $("#conv-list");
  liste.replaceChildren();
  const q = $("#search").value.trim().toLowerCase();
  let g = null;
  etat.convs
    .filter((c) => !q || (c.titre || "").toLowerCase().includes(q))
    .forEach((c) => {
      const gr = groupe(c.maj);
      if (gr !== g) { g = gr; liste.append(el("div", "conv-group", gr)); }
      const item = el("div", "conv" + (etat.conv && etat.conv.id === c.id ? " active" : ""));
      const m = modeDe(c.mode);
      const dot = el("span", "dot");
      dot.style.background = m ? m.couleur : "var(--muted)";
      const actions = el("span", "actions");
      const ren = el("button", null, "✎");
      const del = el("button", null, "✕");
      ren.title = "Renommer";
      del.title = "Supprimer";
      ren.onclick = (e) => { e.stopPropagation(); renommer(c); };
      del.onclick = (e) => { e.stopPropagation(); supprimer(c); };
      actions.append(ren, del);
      item.append(dot, el("span", "name", c.titre || "Sans titre"), actions);
      item.onclick = () => ouvrirConv(c.id);
      liste.append(item);
    });
  const total = etat.convs.reduce((s, c) => s + (c.cout || 0), 0);
  const mesurees = etat.convs.filter((c) => c.cout_sans > 0);
  const reel = mesurees.reduce((s, c) => s + c.cout, 0);
  const sans = mesurees.reduce((s, c) => s + c.cout_sans, 0);
  const n = etat.convs.length;
  let texte = `${n} conversation${n > 1 ? "s" : ""} · ${fmtUSD.format(total)} au total`;
  if (sans > 0) texte += ` · ${Math.round((1 - reel / sans) * 100)} % économisé`;
  $("#total").textContent = texte;
}

async function renommer(c) {
  const titre = prompt("Nouveau titre :", c.titre || "");
  if (titre == null || !titre.trim()) return;
  await api(`/api/conversations/${c.id}`, { method: "PATCH", body: { titre } });
  if (etat.conv && etat.conv.id === c.id) etat.conv.titre = titre.trim();
  await chargerConvs();
  afficherStats();
}

async function supprimer(c) {
  if (!confirm(`Supprimer « ${c.titre || "Sans titre"} » ?`)) return;
  await api(`/api/conversations/${c.id}`, { method: "DELETE" });
  if (etat.conv && etat.conv.id === c.id) nouvelle();
  await chargerConvs();
}

function afficherStats() {
  const c = etat.conv;
  $("#title").textContent = c && c.titre ? c.titre : "Nouvelle conversation";
  const s = $("#stats");
  if (!c || !c.tokens) { s.textContent = ""; return; }
  s.replaceChildren(el("span", null,
    `↑ ${fmtTok(c.tokens.entree)}  ⟳ ${fmtTok(c.tokens.cache)}  ↓ ${fmtTok(c.tokens.sortie)}  ·  ${fmtUSD.format(c.cout)}`));
  s.title = "Tokens envoyés · relus depuis le cache (−90 %) · générés · coût de la conversation";
  if (c.cout_sans > 0) {
    s.append(badgeEco({ pct: 1 - c.cout / c.cout_sans, reel: c.cout, sans: c.cout_sans, r: 0 }));
    s.lastChild.title = `Conversation : ${fmtUSD.format(c.cout)} au lieu de ${fmtUSD.format(c.cout_sans)} sans optimisation`;
  }
}

// --- Messages -----------------------------------------------------------------------

function defiler() {
  const b = $("#messages");
  b.scrollTop = b.scrollHeight;
}

function erreur(texte, code) {
  const e = el("div", "error", texte);
  if (code === "cle") {
    const b = el("button", "btn primary", "Ajouter ma clé");
    b.type = "button";
    b.onclick = () => ouvrirModal("_config");
    e.append(b);
  }
  $("#messages").append(e);
  defiler();
}

function lienExterne(texte, url) {
  const a = el("a", null, texte);
  a.href = url;
  a.target = "_blank";
  return a;
}

async function seConnecter(bouton) {
  bouton.disabled = true;
  bouton.textContent = etat.config.ant_installe ? "Ouverture du navigateur…" : "Installation de l'outil de connexion…";
  try {
    await api("/api/connexion", { method: "POST" });
    etat.config.ant_installe = true;
    bouton.textContent = "Termine la connexion dans ton navigateur…";
    for (let i = 0; i < 90; i++) {  // 3 minutes pour se connecter
      await new Promise((r) => setTimeout(r, 2000));
      const { config } = await api("/api/connexion/verifier", { method: "POST" });
      if (config.cle_definie) {
        etat.cleDefinie = true;
        etat.config = config;
        afficherAccueil();
        return;
      }
    }
    throw new Error("Connexion non terminée. Réessaie.");
  } catch (e) {
    alert(e.message);
    bouton.disabled = false;
    bouton.textContent = "Se connecter avec mon compte Anthropic";
  }
}

function carteCle() {
  const carte = el("form", "cle-card");
  carte.append(el("div", "n", "Connecte Concis à Claude"));

  const compte = el("button", "btn primary", "Se connecter avec mon compte Anthropic");
  compte.type = "button";
  compte.onclick = () => seConnecter(compte);
  carte.append(compte, el("p", "help",
    "Connexion dans le navigateur, comme dans VS Code : aucune clé à copier. "
    + (etat.config.ant_installe ? "" : "Au premier clic, l'outil de connexion officiel d'Anthropic est installé automatiquement.")));

  const input = entree("password", "", { placeholder: "ou colle une clé API : sk-ant-…", autocomplete: "off", required: true });
  const ok = el("button", "btn", "Utiliser la clé");
  ok.type = "submit";
  const ligne = el("div", "cle-row");
  ligne.append(input, ok);
  const aide = el("p", "help");
  aide.append("Clé à créer sur ", lienExterne("console.anthropic.com", "https://console.anthropic.com/settings/keys"),
    ". Elle reste sur cet ordinateur.");
  carte.append(ligne, aide);
  carte.onsubmit = async (e) => {
    e.preventDefault();
    try {
      const { config } = await api("/api/config", { method: "PUT", body: { cle_api: input.value } });
      etat.cleDefinie = config.cle_definie;
      afficherAccueil();
      $("#input").focus();
    } catch (err) { alert(err.message); }
  };
  return carte;
}

function detailEco(e) {
  const l = [`Coût réel : ${fmtUSD.format(e.reel)}`, `Sans optimisation : ${fmtUSD.format(e.sans)}`];
  if (e.cache_lu) l.push(`Cache : ${fmtTok(e.cache_lu)} tokens relus à −90 %`);
  if (e.resume) l.push(`Résumé : ${fmtTok(e.resume)} tokens d'historique non envoyés`);
  if (e.saute) l.push(`Sans historique : ${fmtTok(e.saute)} tokens de contexte non envoyés`);
  if (e.skill) l.push(`Skill : +${fmtTok(e.skill)} tokens envoyés`);
  if (e.r > 0) l.push(`Réponse : ~${fmtTok(e.sortie_evitee)} tokens évités (réduction estimée ${Math.round(e.r * 100)} %)`);
  else l.push("Gain sur la longueur des réponses non compté (Réglages → Réduction de sortie)");
  return l.join("\n");
}

function badgeEco(eco, enCours) {
  const pct = Math.round(eco.pct * 100);
  const texte = pct >= 0 ? `${pct} % économisé` : `${-pct} % de surcoût`;
  const b = el("span", "eco " + (pct > 0 ? "pos" : pct < 0 ? "neg" : ""), texte + (enCours ? " …" : ""));
  b.title = detailEco(eco);
  return b;
}

function ecoEnDirect(base, md) {
  const est = estimer(md);
  const reel = base.reel_entree + (est * base.ps) / 1e6;
  const sans = base.sans_entree + (est / (1 - base.r) * base.ps) / 1e6;
  return { ...base, reel, sans, pct: sans ? 1 - reel / sans : 0, sortie_evitee: Math.round(est / (1 - base.r) - est), est };
}

function metaEl(m) {
  const d = el("div", "meta");
  if (m.meta) {
    if (m.meta.eco) d.append(badgeEco(m.meta.eco));
    d.append(
      el("span", null, `↓ ${fmtTok(m.meta.sortie)} tokens`),
      el("span", null, fmtUSD.format(m.meta.cout)),
      el("span", null, NOMS_MODELES[m.meta.modele] || m.meta.modele),
    );
  }
  const b = el("button", null, "Copier");
  b.type = "button";
  b.onclick = () => copier(m.contenu, b);
  d.append(b);
  return d;
}

function ajouterMessage(m) {
  const box = $("#messages");
  box.querySelector(".empty")?.remove();
  const w = el("div", "msg " + m.role);
  if (m.role === "user") {
    w.append(el("div", "bubble", m.contenu));
    const tags = [LIBELLES[m.consigne], m.isole && "sans historique"].filter(Boolean);
    if (tags.length) {
      const t = el("div", "tags");
      tags.forEach((x) => t.append(el("span", "tag", x)));
      w.append(t);
    }
  } else {
    const c = el("div", "blocs");
    rendreBlocs(c, m.blocs || [{ t: "texte", v: m.contenu }], false);
    w.append(c, metaEl(m));
  }
  box.append(w);
  return w;
}

function afficherAccueil() {
  const box = $("#messages");
  box.replaceChildren();
  const e = el("div", "empty");
  e.append(
    el("h1", null, "Que veux-tu faire ?"),
    el("p", null, "Choisis un mode. Chacun a son propre skill, réglé pour des réponses claires et courtes."),
  );
  const cartes = el("div", "cards");
  etat.modes.forEach((m) => {
    const c = el("button", "card" + (m.id === etat.modeId ? " selected" : ""));
    c.type = "button";
    c.style.setProperty("--c", m.couleur);
    c.append(el("div", "ic", m.icone), el("div", "n", m.nom), el("div", "d", m.description));
    c.onclick = () => choisirMode(m.id);
    cartes.append(c);
  });
  if (!etat.cleDefinie) e.append(carteCle());
  e.append(cartes);
  box.append(e);
}

async function ouvrirConv(id) {
  if (etat.ctrl) return;
  let c;
  try { c = await api(`/api/conversations/${id}`); } catch (e) { erreur(e.message); return; }
  etat.conv = c;
  etat.modeId = c.mode;
  $("#messages").replaceChildren();
  c.messages.forEach((m, i) => {
    if (i === c.debut && c.debut > 0) {
      $("#messages").append(el("div", "notice", `${c.debut} messages précédents résumés pour économiser des tokens`));
    }
    ajouterMessage(m);
  });
  const derniere = [...c.messages].reverse().find((m) => m.role === "assistant");
  etat.contexte = (c.dernier_input || 0) + estimer(derniere ? derniere.contenu : "");
  afficherModeChip();
  afficherStats();
  afficherConvs();
  fermerMenu();
  basculerIsole(false);
  defiler();
}

function nouvelle() {
  if (etat.ctrl) return;
  etat.conv = null;
  etat.contexte = 0;
  etat.isole = false;
  $("#isole").classList.remove("on");
  majCout();
  afficherAccueil();
  afficherModeChip();
  afficherStats();
  afficherConvs();
  fermerMenu();
  $("#input").focus();
}

// --- Projet : dossier de travail du mode Code ---------------------------------------

const LIBELLES_PERMISSIONS = {
  demander: "Demander avant chaque action",
  modifs: "Modifications auto, commandes sur demande",
  tout: "Tout automatique",
};

async function choisirDossier() {
  let chemin = "";
  if (window.pywebview?.api?.choisir_dossier) chemin = await window.pywebview.api.choisir_dossier();
  else chemin = prompt("Chemin complet du dossier du projet :", etat.dossier || "") || "";
  if (!chemin) return;
  try {
    const r = await api("/api/dossier", { method: "POST", body: { chemin } });
    etat.dossier = r.chemin;
    stock.set("dossier", r.chemin);
    if (etat.conv && !etat.conv.dossier) nouvelle(); // un dossier = une nouvelle conversation projet
    afficherProjet();
  } catch (e) { alert(e.message); }
}

function afficherProjet() {
  const barre = $("#projet");
  const m = modeCourant();
  const enCode = m && m.id === "code";
  barre.hidden = !enCode;
  $("#isole").hidden = !!(etat.conv && etat.conv.dossier);
  if (!enCode) return;
  barre.replaceChildren();
  const dossier = etat.conv ? etat.conv.dossier : etat.dossier;

  if (!dossier) {
    const b = el("button", "btn-projet", "📁 Ouvrir un dossier de projet");
    b.type = "button";
    b.onclick = choisirDossier;
    barre.append(b, el("span", "projet-aide", "Claude pourra lire, modifier tes fichiers et lancer des commandes."));
    return;
  }

  const chemin = el("button", "projet-chemin", "📁 " + dossier);
  chemin.type = "button";
  chemin.title = "Ouvrir dans l'Explorateur";
  chemin.onclick = () => api("/api/ouvrir", { method: "POST", body: { chemin: dossier } });
  barre.append(chemin);

  if (!etat.conv) {
    const changer = el("button", "lien", "Changer");
    changer.type = "button";
    changer.onclick = choisirDossier;
    const retirer = el("button", "lien", "Retirer");
    retirer.type = "button";
    retirer.onclick = () => { etat.dossier = ""; stock.set("dossier", ""); afficherProjet(); };
    barre.append(changer, retirer);
  }

  const perm = liste(Object.entries(LIBELLES_PERMISSIONS), etat.conv ? etat.conv.permissions : etat.permissions);
  perm.className = "projet-perm";
  perm.title = "Ce que Claude peut faire sans te demander";
  perm.onchange = async () => {
    if (etat.conv) {
      await api(`/api/conversations/${etat.conv.id}`, { method: "PATCH", body: { permissions: perm.value } });
      etat.conv.permissions = perm.value;
    } else {
      etat.permissions = perm.value;
      stock.set("permissions", perm.value);
    }
  };
  barre.append(el("span", "spacer"), perm);
}

// --- Étapes (outils) et rendu des réponses --------------------------------------------

const ICONES_ETAPE = { null: "◇", modif: "✎", commande: "▶" };
const STATUTS = { ok: "✓", refuse: "refusé", erreur: "erreur", attente: "en attente", en_cours: "…" };

function diffEl(texte) {
  const pre = el("pre", "diff");
  texte.split("\n").forEach((l) => {
    const cls = l.startsWith("+++") || l.startsWith("---") ? "fic"
      : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : "";
    pre.append(el("span", cls, l + "\n"));
  });
  return pre;
}

function carteEtape(b, enCours, rerendre) {
  const carte = el("div", `etape ${b.categorie || "lecture"} ${b.statut}${b.annule ? " annule" : ""}`);
  const tete = el("div", "etape-tete");
  tete.append(
    el("span", "etape-ic", ICONES_ETAPE[b.categorie] || "◇"),
    el("span", "etape-titre", b.titre),
    el("span", "etape-statut", b.annule ? "annulé" : STATUTS[b.statut] || ""),
  );
  carte.append(tete);

  const corps = el("div", "etape-corps");
  if (b.detail) corps.append(b.categorie === "modif" && b.statut !== "erreur" ? diffEl(b.detail) : el("pre", "sortie", b.detail));
  corps.hidden = b.statut !== "attente";
  if (b.detail) {
    tete.classList.add("depliable");
    tete.onclick = () => { corps.hidden = !corps.hidden; };
  }

  if (b.statut === "attente") {
    const actions = el("div", "etape-actions");
    const repondre = async (decision) => {
      actions.querySelectorAll("button").forEach((x) => (x.disabled = true));
      try { await api(`/api/approbations/${b.id}`, { method: "POST", body: { decision } }); }
      catch (e) { alert(e.message); }
    };
    const oui = el("button", "btn primary", "Autoriser");
    const toujours = el("button", "btn", b.categorie === "commande" ? "Toujours (commandes)" : "Toujours (modifications)");
    const non = el("button", "btn danger", "Refuser");
    [oui, toujours, non].forEach((x) => (x.type = "button"));
    oui.onclick = () => repondre("oui");
    toujours.onclick = () => repondre("toujours");
    non.onclick = () => repondre("non");
    actions.append(oui, toujours, non);
    corps.append(actions);
  } else if (b.categorie === "modif" && b.statut === "ok" && !b.annule && !enCours && etat.conv) {
    const annuler = el("button", "lien", "Annuler cette modification");
    annuler.type = "button";
    annuler.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Annuler « ${b.titre} » ?`)) return;
      try {
        await api(`/api/conversations/${etat.conv.id}/annuler/${b.id}`, { method: "POST" });
        b.annule = true;
        rerendre();
      } catch (err) { alert(err.message); }
    };
    tete.append(annuler);
  }
  carte.append(corps);
  return carte;
}

function rendreBlocs(conteneur, blocs, enCours) {
  conteneur.replaceChildren();
  const rerendre = () => rendreBlocs(conteneur, blocs, enCours);
  blocs.forEach((b) => {
    if (b.t === "texte") {
      const c = el("div", "content");
      c.innerHTML = rendre(b.v);
      if (!enCours) decorer(c);
      conteneur.append(c);
    } else {
      conteneur.append(carteEtape(b, enCours, rerendre));
    }
  });
}

// --- Envoi et réception en flux ---------------------------------------------------

function majBouton() {
  const b = $("#send");
  const enCours = !!etat.ctrl;
  b.classList.toggle("stop", enCours);
  b.textContent = enCours ? "■" : "↑";
  b.title = enCours ? "Arrêter" : "Envoyer (Entrée)";
  b.disabled = !enCours && !$("#input").value.trim();
  majCout();
}

function ajuster() {
  const t = $("#input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 240) + "px";
  majBouton();
}

async function envoyer() {
  if (etat.ctrl) {
    etat.ctrl.abort();
    if (etat.conv) api(`/api/conversations/${etat.conv.id}/arreter`, { method: "POST" }).catch(() => {});
    return;
  }
  const input = $("#input");
  const texte = input.value.trim();
  if (!texte) return;

  try {
    if (!etat.conv) {
      const corps = { mode: etat.modeId };
      if (etat.modeId === "code" && etat.dossier) Object.assign(corps, { dossier: etat.dossier, permissions: etat.permissions });
      etat.conv = await api("/api/conversations", { method: "POST", body: corps });
      afficherProjet();
    }
  } catch (e) { erreur(e.message); return; }

  const options = { consigne: etat.longueur || undefined, isole: etat.isole && etat.conv.tokens.sortie > 0 };
  input.value = "";
  ajouterMessage({ role: "user", contenu: texte, consigne: options.consigne, isole: options.isole });
  const w = el("div", "msg assistant");
  const c = el("div", "blocs cursor");
  const direct = el("div", "meta");
  w.append(c, direct);
  $("#messages").append(w);
  defiler();

  const ctrl = new AbortController();
  etat.ctrl = ctrl;
  ajuster();

  const blocs = [];  // texte et étapes, dans l'ordre d'arrivée
  let md = "";
  let fin = null;
  let base = null; // coûts côté entrée, reçus au début du flux
  let planifie = false;
  const majDirect = () => {
    if (!base) return;
    const eco = ecoEnDirect(base, md);
    direct.replaceChildren(badgeEco(eco, true), el("span", null, `↓ ~${fmtTok(eco.est)} tokens`));
  };
  const planifier = () => {
    if (planifie) return;
    planifie = true;
    requestAnimationFrame(() => {
      planifie = false;
      const b = $("#messages");
      const enBas = b.scrollHeight - b.scrollTop - b.clientHeight < 120;
      rendreBlocs(c, blocs, true);
      majDirect();
      if (enBas) defiler();
    });
  };
  const majEtape = (e) => {
    const i = blocs.findIndex((x) => x.id === e.id);
    if (i >= 0) blocs[i] = e; else blocs.push(e);
    planifier();
  };

  try {
    const r = await fetch(`/api/conversations/${etat.conv.id}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texte, ...options }),
      signal: ctrl.signal,
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.erreur || r.statusText);
    }
    const lecteur = r.body.getReader();
    const dec = new TextDecoder();
    let tampon = "";
    for (;;) {
      const { done, value } = await lecteur.read();
      if (done) break;
      tampon += dec.decode(value, { stream: true });
      let i;
      while ((i = tampon.indexOf("\n\n")) >= 0) {
        const bloc = tampon.slice(0, i);
        tampon = tampon.slice(i + 2);
        const ev = /^event: (.*)$/m.exec(bloc)?.[1];
        const data = JSON.parse(/^data: (.*)$/m.exec(bloc)?.[1] || "{}");
        if (ev === "delta") {
          md += data.t;
          if (blocs.length && blocs[blocs.length - 1].t === "texte") blocs[blocs.length - 1].v += data.t;
          else blocs.push({ t: "texte", v: data.t });
          planifier();
        }
        else if (ev === "demande") majEtape({ ...data, statut: "attente" });
        else if (ev === "etape") majEtape(data);
        else if (ev === "permissions") { etat.conv.permissions = data.permissions; afficherProjet(); }
        else if (ev === "debut") { base = data; majDirect(); }
        else if (ev === "fin") {
          fin = data;
          md = data.texte;
          blocs.splice(0, blocs.length, ...(data.blocs || [{ t: "texte", v: data.texte }]));
        }
        else if (ev === "erreur") erreur(data.message, data.code);
        else if (ev === "resume") {
          const n = el("div", "notice", `${data.messages_resumes} messages précédents résumés pour économiser des tokens`);
          (w.previousElementSibling || w).before(n);
        }
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") erreur(e.message);
  } finally {
    etat.ctrl = null;
    c.classList.remove("cursor");
    direct.remove();
    if (blocs.length) {
      if (!fin && md && !blocs.some((b) => b.t !== "texte")) blocs.splice(0, blocs.length, { t: "texte", v: md });
      rendreBlocs(c, blocs.filter((b) => b.statut !== "attente"), false);
      w.append(metaEl({ contenu: md, meta: fin && fin.meta }));
    } else {
      w.remove();
    }
    if (fin) {
      Object.assign(etat.conv, fin.conv);
      // Prochain envoi : le contexte de cette requête, plus la réponse (et la question isolée, qui rejoint l'historique).
      etat.contexte = fin.conv.contexte + estimer(md) + (options.isole ? estimer(texte) : 0);
    }
    basculerIsole(false);
    try {
      await chargerConvs();
      const s = etat.convs.find((x) => x.id === etat.conv.id);
      if (s) Object.assign(etat.conv, s);
    } catch { /* liste rafraîchie au prochain envoi */ }
    afficherStats();
    ajuster();
    defiler();
  }
}

// --- Fenêtre « Modes & réglages » ---------------------------------------------------

let selection = "_base";

function champ(label, input, aide) {
  const d = el("div", "field");
  d.append(el("label", null, label), input);
  if (aide) d.append(el("p", "help", aide));
  return d;
}

function entree(type, valeur, attrs = {}) {
  const i = el(type === "textarea" ? "textarea" : "input");
  if (type !== "textarea") i.type = type;
  i.value = valeur ?? "";
  Object.assign(i, attrs);
  return i;
}

function liste(options, valeur) {
  const s = el("select");
  options.forEach(([v, t]) => {
    const o = el("option", null, t);
    o.value = v;
    o.selected = v === valeur;
    s.append(o);
  });
  return s;
}

function actionsForm(f, enregistrer, supprimerFn, restaurable) {
  const a = el("div", "form-actions");
  const ok = el("span", "saved", "Enregistré ✓");
  a.append(ok);
  if (restaurable) {
    const r = el("button", "btn", "Rétablir par défaut");
    r.type = "button";
    r.title = "Remplace ce skill par la version livrée avec l'application";
    r.onclick = async () => {
      if (!confirm("Remplacer par la version par défaut ? Tes modifications seront perdues.")) return;
      try {
        await api(`/api/modes/${restaurable}/restaurer`, { method: "POST" });
        await chargerModes();
        if (restaurable === "_base") etat.base = (await api("/api/base")).contenu;
        construireNav();
        afficherForm();
        rafraichirApresModes();
      } catch (e) { alert(e.message); }
    };
    a.append(r);
  }
  if (supprimerFn) {
    const d = el("button", "btn danger", "Supprimer");
    d.type = "button";
    d.onclick = () => supprimerFn().catch((e) => alert(e.message));
    a.append(d);
  }
  const s = el("button", "btn primary", "Enregistrer");
  s.type = "submit";
  a.append(s);
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await enregistrer();
      ok.classList.add("on");
      setTimeout(() => ok.classList.remove("on"), 1500);
    } catch (err) { alert(err.message); }
  };
  f.append(a);
}

function construireNav() {
  const nav = $("#modal-nav");
  nav.replaceChildren();
  const item = (id, ic, nom, couleur) => {
    const b = el("button", "nav-item" + (id === selection ? " active" : ""));
    b.type = "button";
    if (couleur) b.style.setProperty("--c", couleur);
    b.append(el("span", "ic", ic), el("span", null, nom));
    b.onclick = () => { selection = id; construireNav(); afficherForm(); };
    nav.append(b);
  };
  item("_base", "≡", "Règles communes");
  item("_config", "⚙", "Réglages");
  nav.append(el("div", "nav-sep"));
  etat.modes.forEach((m) => item(m.id, m.icone, m.nom, m.couleur));
  item("_nouveau", "＋", "Nouveau mode");
}

async function afficherForm() {
  const f = $("#modal-form");
  f.replaceChildren();
  f.onsubmit = null;
  if (selection === "_base") return formBase(f);
  if (selection === "_config") return formConfig(f);
  const nouveau = selection === "_nouveau";
  const m = nouveau
    ? { id: "", nom: "", icone: "•", couleur: "#8b5cf6", modele: "claude-opus-5-5", effort: "low", max_tokens: 8000, ordre: 99, description: "", contenu: "" }
    : modeDe(selection);
  formMode(f, m, nouveau);
}

async function formBase(f) {
  const { contenu } = await api("/api/base");
  const t = entree("textarea", contenu);
  const est = el("div", "estimate");
  const maj = () => { est.innerHTML = `≈ <b>${estimer(t.value)}</b> tokens ajoutés à chaque message, dans tous les modes.`; };
  t.oninput = maj;
  maj();
  f.append(
    el("p", "help", "Ces règles s'appliquent à tous les modes, avant le skill du mode. Plus elles sont courtes, moins chaque message coûte."),
    champ("Règles communes", t),
    est,
  );
  actionsForm(f, async () => {
    await api("/api/base", { method: "PUT", body: { contenu: t.value } });
    etat.base = t.value;
    majCout();
  }, null, "_base");
}

async function formConfig(f) {
  const { config: c, modeles } = await api("/api/config");
  const mod = liste(modeles.map((m) => [m, NOMS_MODELES[m] || m]), c.modele_resume);
  const seuil = entree("number", c.seuil_resume, { min: 1000, step: 500 });
  const garde = entree("number", c.messages_gardes, { min: 2, max: 40 });
  const reduc = entree("number", Math.round(c.reduction_sortie * 100), { min: 0, max: 90, step: 1 });
  const ligne = el("div", "row");
  ligne.append(
    champ("Seuil de résumé (tokens)", seuil, "Taille du contexte au-delà de laquelle les anciens messages sont résumés."),
    champ("Messages gardés intacts", garde, "Derniers messages toujours envoyés en entier."),
  );
  const calcul = champ("Réduction de sortie (%)", reduc,
    "Sert au calcul du « % économisé ». Le cache, le résumé et le coût du skill sont mesurés à chaque message. "
    + "La réduction de longueur des réponses ne peut pas l'être sans payer une seconde réponse : "
    + "indique ici la réduction moyenne mesurée par l'optimiseur (rapport.md). À 0, seuls les gains mesurés sont comptés.");
  const cle = entree("password", "", {
    placeholder: c.cle_definie ? `Clé enregistrée (${c.cle_source}) — laisser vide pour la garder` : "sk-ant-…",
    autocomplete: "off",
  });
  const aideCle = c.cle_definie
    ? "Une clé est déjà configurée. En saisir une nouvelle la remplace."
    : "Aucune clé détectée. Crée-la sur console.anthropic.com. Elle reste sur cet ordinateur (donnees/config.json).";
  etat.config = { ...etat.config, ...c };
  const compte = el("button", "btn", c.cle_source === "compte Anthropic" ? "Reconnecter mon compte Anthropic" : "Se connecter avec mon compte Anthropic");
  compte.type = "button";
  compte.style.alignSelf = "flex-start";
  compte.onclick = async () => { await seConnecter(compte); afficherForm(); };
  f.append(compte, champ("Ou clé API Anthropic", cle, aideCle));
  if (c.cle_source === "clé enregistrée") {
    const eff = el("button", "btn danger", "Supprimer la clé enregistrée");
    eff.type = "button";
    eff.style.alignSelf = "flex-start";
    eff.onclick = async () => {
      if (!confirm("Supprimer la clé API enregistrée ?")) return;
      await api("/api/config", { method: "PUT", body: { effacer_cle: true } });
      afficherForm();
    };
    f.append(eff);
  }
  f.append(
    el("p", "help", "Résumé automatique : quand une conversation s'allonge, les anciens échanges sont remplacés par des notes courtes. Chaque nouveau message coûte alors beaucoup moins."),
    champ("Modèle des résumés", mod, "Haiku 4.5 est le moins cher, et un résumé est un travail simple."),
    ligne,
    calcul,
  );
  actionsForm(f, async () => {
    await api("/api/config", {
      method: "PUT",
      body: {
        modele_resume: mod.value, seuil_resume: +seuil.value, messages_gardes: +garde.value,
        reduction_sortie: +reduc.value / 100, cle_api: cle.value,
      },
    });
    if (cle.value) afficherForm();
  });
}

function formMode(f, m, nouveau) {
  const id = entree("text", m.id, { placeholder: "ex : marketing", pattern: "[a-z0-9][a-z0-9_\\-]{0,31}", required: true });
  const nom = entree("text", m.nom, { required: true, maxLength: 30 });
  const ic = entree("text", m.icone, { maxLength: 3 });
  const couleur = entree("color", m.couleur);
  const modele = liste(["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"].map((k) => [k, NOMS_MODELES[k]]), m.modele);
  const effort = liste([["low", "Bas (le plus économe)"], ["medium", "Moyen"], ["high", "Élevé"]], m.effort);
  const majEffort = () => { effort.disabled = modele.value.startsWith("claude-haiku"); };
  modele.onchange = majEffort;
  majEffort();
  const ordre = entree("number", m.ordre, { min: 1, max: 99 });
  const desc = entree("text", m.description, { maxLength: 80 });
  const contenu = entree("textarea", m.contenu);
  const est = el("div", "estimate");
  const majEst = () => { est.innerHTML = `Skill du mode : ≈ <b>${estimer(contenu.value)}</b> tokens ajoutés à chaque message.`; };
  contenu.oninput = majEst;
  majEst();

  if (nouveau) f.append(champ("Identifiant", id, "Minuscules, chiffres, - et _. Non modifiable ensuite."));
  const l1 = el("div", "row");
  l1.append(champ("Nom", nom), champ("Icône", ic), champ("Couleur", couleur));
  const l2 = el("div", "row");
  l2.append(
    champ("Modèle", modele),
    champ("Effort de réflexion", effort),
    champ("Ordre", ordre),
  );
  f.append(
    l1, l2,
    el("p", "help", "Effort bas : moins de tokens de réflexion facturés. Garde « Moyen » pour le code et l'analyse. Haiku n'a pas de réglage d'effort. Aucune limite de longueur : seuls les tokens réellement écrits sont facturés."),
    champ("Description", desc),
    champ("Skill du mode", contenu),
    est,
  );

  actionsForm(
    f,
    async () => {
      const mid = nouveau ? id.value.trim() : m.id;
      await api(`/api/modes/${mid}`, {
        method: "PUT",
        body: {
          nom: nom.value, icone: ic.value, couleur: couleur.value, modele: modele.value,
          effort: effort.value, ordre: +ordre.value,
          description: desc.value, contenu: contenu.value,
        },
      });
      await chargerModes();
      selection = mid;
      construireNav();
      if (nouveau) afficherForm();
      rafraichirApresModes();
    },
    nouveau || m.id === "general" ? null : async () => {
      if (!confirm(`Supprimer le mode « ${m.nom} » ?`)) return;
      await api(`/api/modes/${m.id}`, { method: "DELETE" });
      await chargerModes();
      selection = "_base";
      construireNav();
      afficherForm();
      rafraichirApresModes();
    },
    !nouveau && m.par_defaut ? m.id : null,
  );
}

function rafraichirApresModes() {
  afficherModeChip();
  afficherConvs();
  if (!etat.conv) afficherAccueil();
}

async function ouvrirModal(cible) {
  if (typeof cible === "string") selection = cible;
  try { await chargerModes(); } catch (e) { erreur(e.message); return; }
  construireNav();
  afficherForm();
  $("#modal").showModal();
}

// --- Menu mobile --------------------------------------------------------------------

function fermerMenu() { $("#app").classList.remove("menu-open"); }

// --- Démarrage ------------------------------------------------------------------------

(async function init() {
  appliquerTheme(stock.get("theme"));

  $("#btn-theme").onclick = basculerTheme;
  $("#btn-new").onclick = nouvelle;
  $("#btn-modes").onclick = ouvrirModal;
  $("#btn-menu").onclick = () => $("#app").classList.toggle("menu-open");
  $("#scrim").onclick = fermerMenu;
  $("#search").oninput = afficherConvs;
  $("#mode-btn").onclick = basculerMenuModes;
  $("#send").onclick = envoyer;
  $("#isole").onclick = () => basculerIsole();
  document.querySelectorAll("#longueur button").forEach((b) => {
    b.onclick = () => {
      etat.longueur = b.dataset.v;
      stock.set("longueur", etat.longueur);
      majLongueur();
      $("#input").focus();
    };
  });
  majLongueur();
  $("#modal-close").onclick = () => $("#modal").close();
  $("#modal").addEventListener("click", (e) => { if (e.target === $("#modal")) $("#modal").close(); });

  const input = $("#input");
  input.addEventListener("input", ajuster);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (!etat.ctrl) envoyer();
    }
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".mode-picker")) $("#mode-menu").hidden = true;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $("#mode-menu").hidden = true;
  });

  $("#modal").addEventListener("close", async () => {
    try {
      etat.config = (await api("/api/config")).config;
      etat.cleDefinie = etat.config.cle_definie;
    } catch { /* sans effet */ }
    if (!etat.conv) afficherAccueil();
  });

  try {
    await chargerModes();
    await chargerConvs();
    etat.config = (await api("/api/config")).config;
    etat.cleDefinie = etat.config.cle_definie;
    etat.base = (await api("/api/base")).contenu;
  } catch (e) {
    erreur("Serveur injoignable : " + e.message);
  }
  nouvelle();
  ajuster();
})();
