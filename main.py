import streamlit as st
import openai
import re

# --- UTILS

def lire_strings_xml(xml_content):
    mapping = {}
    for m in re.finditer(r'<string name="([^"]+)">(.*?)</string>', xml_content, re.DOTALL):
        cle = m.group(1)
        txt = m.group(2)
        mapping[txt.strip()] = cle.strip()
    return mapping

def clean_openai_code(text):
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        if line.strip().startswith("```"):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)

def block_is_complete(line):
    line = line.strip()
    return line.endswith(";") or line.endswith("}") or line.endswith("])") or line.endswith("});")

def remove_duplicate_overlap(blocks):
    result = []
    for i, block in enumerate(blocks):
        if i > 0:
            prev_lines = result[-1].splitlines()
            curr_lines = block.splitlines()
            if prev_lines and curr_lines and prev_lines[-1].strip() == curr_lines[0].strip():
                block = "\n".join(curr_lines[1:])
        result.append(block)
    return "\n".join(result)

def clean_incomplete_lines(code):
    """ Fusionne les lignes cassées classiques (getString... etc) et signale les warnings. """
    lines = code.splitlines()
    clean = []
    warnings = []
    i = 0
    while i < len(lines):
        l = lines[i]
        m = re.match(r'^(.*getString\(R\.string\.[a-zA-Z0-9_]*)( *[\+\)]*)?$', l.strip())
        if m and not l.strip().endswith(");"):
            if i+1 < len(lines):
                next_l = lines[i+1].strip()
                merged = l.rstrip() + next_l
                if (merged.count('(') == merged.count(')')) and merged.endswith(");"):
                    clean.append(merged)
                    i += 2
                    continue
            warnings.append(f"⚠️ Ligne incomplète potentielle (getString) : {l.strip()}")
            clean.append(l)
            i += 1
            continue
        if l.strip().endswith("(") or l.strip().endswith("+") or l.strip().endswith("."):
            warnings.append(f"⚠️ Ligne possiblement coupée : {l.strip()}")
        clean.append(l)
        i += 1
    return "\n".join(clean), warnings

def verifier_coupures(code):
    """ Repère toutes les lignes suspectes ou coupées dans le code. """
    warnings = []
    lines = code.splitlines()
    for i, l in enumerate(lines):
        stripped = l.strip()
        if re.match(r'.*getString\(R\.string\.[a-zA-Z0-9_]*$', stripped):
            warnings.append(f"Ligne {i+1}: getString cassé → {stripped}")
        if (stripped.endswith(".") or stripped.endswith("+")) and not stripped.endswith(";"):
            warnings.append(f"Ligne {i+1}: fin anormale → {stripped}")
        if (stripped.count("(") > stripped.count(")")) or (stripped.count("{") > stripped.count("}")):
            if not (stripped.endswith("{") or stripped.endswith("(")):
                warnings.append(f"Ligne {i+1}: parenthèse/accolade probablement non fermée → {stripped}")
    return warnings

def verifier_et_corriger_strings_coupees(code):
    """ Détecte les strings coupées avant une déclaration de méthode/var et propose une correction """
    warnings = []
    corrections = []
    lines = code.splitlines()
    for i, l in enumerate(lines):
        stripped = l.strip()
        # Si la ligne contient un guillemet ouvert (mais pas fermé), ET la suivante est une déclaration Java
        if '"' in stripped and not re.search(r'".*"$', stripped) and not stripped.endswith('",') and not stripped.endswith('"+') and not stripped.endswith('");'):
            if i+1 < len(lines):
                next_line = lines[i+1].strip()
                if re.match(r'^(private|public|protected|static)', next_line):
                    warnings.append(f"Ligne {i+1}: String probablement coupée avant déclaration : {stripped}")
                    # Suggestion de correction : fermer la string et terminer proprement la ligne
                    suggestion = stripped + '...<ajouter fermeture de guillemet, + ou , ou ); selon le contexte>'
                    corrections.append(f"Corrige ligne {i+1} :\n{suggestion}")
    return warnings, corrections

def reindent_and_clean_java(code):
    """
    - Enlève les redondances (package, import, classe)
    - Réindente les case/break/default dans un switch
    - Nettoie accolades surnuméraires typiques de l’IA
    """
    lines = code.splitlines()
    new_lines = []
    seen_package = False
    seen_class = False
    imports = set()
    inside_switch = False
    indent_switch = " " * 12   # adapte l'indentation à ta structure réelle
    for l in lines:
        stripped = l.lstrip()
        # Supprimer package sauf tout premier
        if stripped.startswith("package "):
            if not seen_package:
                new_lines.append(stripped)
                seen_package = True
            continue
        # Supprimer imports en double
        if stripped.startswith("import "):
            if stripped not in imports:
                new_lines.append(stripped)
                imports.add(stripped)
            continue
        # Une seule fois la déclaration de classe
        if stripped.startswith("public class CourseUtils"):
            if not seen_class:
                new_lines.append(stripped)
                seen_class = True
            continue
        # Corriger indentation switch/case
        if "switch" in stripped:
            inside_switch = True
            new_lines.append(l)
            continue
        if inside_switch and (stripped.startswith("case ") or stripped.startswith("default:")):
            new_lines.append(indent_switch + stripped)
            continue
        if inside_switch and stripped.startswith("break;"):
            new_lines.append(indent_switch + stripped)
            continue
        # Ferme le switch
        if inside_switch and stripped == "}":
            inside_switch = False
            new_lines.append(l)
            continue
        # Évite les accolades inutiles seules
        if stripped in ["{", "}"]:
            # Garde seulement si elle entoure vraiment une structure
            if new_lines and (new_lines[-1].strip().endswith(")") or new_lines[-1].strip().endswith("{")):
                new_lines.append(l)
            continue
        new_lines.append(l)
    # Enlève les lignes vides multiples
    clean_final = []
    last_was_empty = False
    for ln in new_lines:
        if ln.strip() == "":
            if last_was_empty:
                continue
            last_was_empty = True
        else:
            last_was_empty = False
        clean_final.append(ln)
    return "\n".join(clean_final)

# --- STREAMLIT PAGE CONFIG
st.set_page_config(page_title="Java Refactor Internationalizer", layout="wide")

# --- SESSION STATE INIT
if "run_state" not in st.session_state:
    st.session_state.run_state = "stopped"
if "log_lines" not in st.session_state:
    st.session_state.log_lines = []
if "results" not in st.session_state:
    st.session_state.results = []
if "current_block" not in st.session_state:
    st.session_state.current_block = 0
if "raw_api_responses" not in st.session_state:
    st.session_state.raw_api_responses = []

# --- SIDEBAR : CONFIG
st.sidebar.header("Configuration")

api_key = st.sidebar.text_input("OpenAI API Key", type="password", key="api_key")
lines_per_block = st.sidebar.number_input("Lignes par bloc", 10, 500, 50)

uploaded_java = st.sidebar.file_uploader("Fichier Java source", type=["java"])
uploaded_xml = st.sidebar.file_uploader("strings.xml", type=["xml"])

default_prompt = (
    "Tu es un assistant Android expert en refactoring de code pour l'internationalisation.\n"
    "Je vais te donner un bloc de code Java. Dans ce code, toutes les chaînes affichées à l'utilisateur"
    "(via setText, Toast, AlertDialog, etc.) sont déjà présentes dans mon strings.xml anglais.\n"
    "Remplace chaque chaîne affichée par l'appel à la ressource correspondante :\n"
    " - Si c'est dans une Activity : getString(R.string.nom_de_la_chaine)\n"
    " - Sinon (ex: Adapter, Helper) : context.getString(R.string.nom_de_la_chaine).\n"
    "Utilise exactement la clé qui correspond au texte affiché, d'après mon strings.xml fourni ci-après.\n"
    "Ne modifie rien d'autre dans le code. Donne-moi uniquement le code Java modifié, sans explication, sans balise.\n"
    "N'ajoute jamais ``` ou ```java ou d'autres balises dans ta réponse."
)

prompt_system = st.sidebar.text_area("Prompt système (modifiable à la volée)", default_prompt, height=180)

# --- CONTROLS : BOUTONS
st.sidebar.markdown("---")
col1, col2, col3, col4 = st.sidebar.columns([1,1,1,1])
if col1.button("▶️ Démarrer/Reprendre"):
    st.session_state.run_state = "running"
if col2.button("⏸️ Pause"):
    st.session_state.run_state = "paused"
if col3.button("⏹️ Stop"):
    st.session_state.run_state = "stopped"
if col4.button("🔄 Réinit."):
    st.session_state.run_state = "stopped"
    st.session_state.results = []
    st.session_state.current_block = 0
    st.session_state.log_lines = []
    st.session_state.raw_api_responses = []

# --- HEADER + STATUS
st.title("☕ Refactor Java → Android Internationalization")
status_color = (
    "green" if st.session_state.run_state=="running" else
    "orange" if st.session_state.run_state=="paused" else
    "red"
)
status_label = (
    "EN COURS" if st.session_state.run_state=="running" else
    "EN PAUSE" if st.session_state.run_state=="paused" else
    "STOPPÉ"
)
st.markdown(
    f"<h4 style='color:{status_color};display:inline'>Statut : {status_label}</h4>",
    unsafe_allow_html=True
)

# --- MAIN
if uploaded_java and uploaded_xml and api_key:
    java_lines = uploaded_java.getvalue().decode("utf-8").splitlines(keepends=True)
    xml_content = uploaded_xml.getvalue().decode("utf-8")
    mapping = lire_strings_xml(xml_content)
    extrait_strings = "\n".join(f"{v} => {k}" for k, v in list(mapping.items())[:80])
    prompt_user_base = (
        "Voici un extrait du fichier strings.xml (clé => texte) :\n"
        + extrait_strings +
        "\n\nVoici un bloc de code Java à traiter :\n"
    )

    # Construction des blocs AVEC overlap
    blocks = []
    i = 0
    overlap = ""
    while i < len(java_lines):
        bloc = []
        if overlap:
            bloc.append(overlap)
        bloc += java_lines[i:i+lines_per_block]
        if bloc:
            last_line = bloc[-1]
            if not block_is_complete(last_line):
                overlap = last_line.rstrip("\n")
                bloc = bloc[:-1]
            else:
                overlap = ""
        blocks.append("".join(bloc))
        i += lines_per_block

    n_blocks = len(blocks)
    output_blocks = st.session_state.results or [""] * n_blocks
    current_block = st.session_state.current_block
    raw_api_responses = st.session_state.raw_api_responses or [None] * n_blocks

    # --- Status, progression & logs
    st.info(f"Traitement bloc {current_block+1}/{n_blocks}")
    st.progress(current_block / n_blocks if n_blocks > 0 else 0.0)

    log_placeholder = st.empty()
    code_placeholder = st.empty()

    # --- CODE EN COURS DE CONSTRUCTION (fusionné)
    code_final_raw = remove_duplicate_overlap(output_blocks)
    code_final, warnings_auto = clean_incomplete_lines(code_final_raw)
    warnings_supp = verifier_coupures(code_final)
    warnings_string, corrections_string = verifier_et_corriger_strings_coupees(code_final)
    all_warnings = list(set(warnings_auto + warnings_supp + warnings_string))

    # ---- AJOUTE ICI LA REINDENTATION & NETTOYAGE
    code_final = reindent_and_clean_java(code_final)

    code_placeholder.code(code_final, language="java")

    # --- WARNINGS & CORRECTIONS
    if all_warnings:
        st.warning(f"🚨 Coupures/alertes trouvées ({len(all_warnings)}) dans le code généré :")
        for w in all_warnings:
            st.text(w)
    else:
        st.success("✅ Aucun pattern suspect détecté dans le code généré.")

    if corrections_string:
        st.error("💡 Propositions de correction (strings coupées):")
        for corr in corrections_string:
            st.text(corr)

    # --- LOGS EN LIVE
    st.subheader("Console logs (API, erreurs, status)")
    log_placeholder.code("\n".join(st.session_state.log_lines[-40:]), language="shell")

    # --- TRAITEMENT EN PAS A PAS
    if st.session_state.run_state == "running":
        if current_block < n_blocks:
            prompt = prompt_user_base + blocks[current_block]
            try:
                st.session_state.log_lines.append(f"Bloc {current_block+1}/{n_blocks} : appel GPT…")
                openai.api_key = api_key
                response = openai.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": prompt_system},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.1,
                    max_tokens=2000
                )
                st.session_state.raw_api_responses.append(response)
                out = clean_openai_code(response.choices[0].message.content)
                output_blocks[current_block] = out
                st.session_state.log_lines.append(f"Bloc {current_block+1}/{n_blocks} traité ✓")
                st.session_state.log_lines.append("--- Réponse GPT (début) ---")
                st.session_state.log_lines.append(out[:600] + ("..." if len(out) > 600 else ""))
                st.session_state.current_block += 1
                st.session_state.results = output_blocks
                st.session_state.raw_api_responses = raw_api_responses
                st.rerun()
            except Exception as e:
                st.session_state.log_lines.append(f"Erreur bloc {current_block+1}: {e}")
                st.session_state.run_state = "paused"
        else:
            st.session_state.run_state = "stopped"
            st.success("Tous les blocs sont traités !")

    # --- BLOCS RÉPONSE "LONG" + DEBUG CHAT
    st.subheader("🗃️ Réponses GPT brutes par bloc (mode debug/chat)")
    for i, (bloc_txt, raw_json) in enumerate(zip(output_blocks, st.session_state.raw_api_responses)):
        with st.expander(f"Bloc {i+1}/{n_blocks} - Voir la réponse complète (cliquer pour déplier)", expanded=False):
            st.code(bloc_txt or "Non traité", language="java")
            if raw_json:
                st.text_area(
                    label=f"Réponse API JSON bloc {i+1} (debug chat complet)",
                    value=str(raw_json),
                    height=160,
                    key=f"raw_json_{i}",
                    disabled=True
                )

    # --- TÉLÉCHARGEMENT DU CODE FINAL
    st.download_button("📥 Télécharger le code final", code_final, file_name="coursutils_modifie.java", mime="text/x-java-source")
else:
    st.info("Importe ton fichier .java et ton strings.xml et renseigne ta clé OpenAI pour commencer.")
