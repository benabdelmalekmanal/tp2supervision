"""Supervision d'un atelier : référentiel des machines, relevés capteurs, alertes et rapport."""
import csv
import json
import math
import re
from datetime import datetime
from pathlib import Path

from exceptions import FichierInvalide, ReleveInvalide

COLONNES = ("machine", "horodatage", "temperature", "vibration")

# Un relevé valide est un tuple : (machine, horodatage, temperature, vibration)
Releve = tuple[str, datetime, float, float]


# --- Étape 1 : le référentiel des machines (JSON, dictionnaires, ensembles) --------------------
def charger_machines(chemin: Path) -> dict[str, dict]:
    """Lit machines.json et renvoie un dictionnaire {identifiant: fiche de la machine}.

    Lève FichierInvalide si un identifiant apparaît deux fois.
    """
    try:
        with chemin.open(encoding="utf-8") as fichier:
            fiches = json.load(fichier)
    except json.JSONDecodeError as erreur:
        raise FichierInvalide(f"JSON invalide dans {chemin} : {erreur}") from erreur

    machines: dict[str, dict] = {}
    for fiche in fiches:
        identifiant = fiche["id"]
        if identifiant in machines:
            raise FichierInvalide(f"identifiant en double : {identifiant}")
        machines[identifiant] = fiche
    return machines


def ateliers(machines: dict[str, dict]) -> set[str]:
    """L'ensemble des ateliers où se trouvent les machines."""
    return {fiche["atelier"] for fiche in machines.values()}


# --- Étape 3 : valider une ligne (exceptions, tuples) -------------------------------------------
def lire_nombre(texte: str, champ: str, numero: int) -> float:
    """Convertit "52,3" en 52.3 ; lève ReleveInvalide si la valeur est vide ou non numérique."""
    valeur = texte.strip()
    if not valeur:
        raise ReleveInvalide("valeur manquante", numero, champ)

    try:
        nombre = float(valeur.replace(",", "."))
    except ValueError as erreur:
        raise ReleveInvalide(f"non numérique : '{valeur}'", numero, champ) from erreur

    if not math.isfinite(nombre):
        raise ReleveInvalide("valeur non finie", numero, champ)
    return nombre


def parser_releve(brut: dict[str, str], numero: int, machines: dict[str, dict]) -> Releve:
    """Transforme une ligne brute du CSV (dictionnaire de textes) en relevé typé.

    Lève ReleveInvalide (avec le numéro de ligne et le champ fautif) au premier problème.
    """
    identifiant = (brut.get("machine") or "").strip().upper()
    if not identifiant:
        raise ReleveInvalide("valeur manquante", numero, "machine")
    if identifiant not in machines:
        raise ReleveInvalide(f"machine inconnue : '{identifiant}'", numero, "machine")

    texte_horodatage = (brut.get("horodatage") or "").strip()
    try:
        horodatage = datetime.strptime(texte_horodatage, "%Y-%m-%d %H:%M")
    except ValueError as erreur:
        raise ReleveInvalide(
            f"horodatage invalide : '{texte_horodatage}'", numero, "horodatage"
        ) from erreur

    temperature = lire_nombre(brut.get("temperature") or "", "temperature", numero)
    vibration = lire_nombre(brut.get("vibration") or "", "vibration", numero)
    if vibration < 0:
        raise ReleveInvalide(
            f"valeur négative : '{(brut.get('vibration') or '').strip()}'", numero, "vibration"
        )
    return identifiant, horodatage, temperature, vibration


# --- Étape 4 : lire le fichier (CSV, générateur, collecte des erreurs) ---------------------------
def lignes_csv(chemin: Path):
    """Générateur : produit (numero, ligne_brute) pour chaque ligne de données du CSV.

    Le numéro est celui de la ligne dans le fichier (l'en-tête est la ligne 1).
    Lève FichierInvalide si une colonne attendue manque.
    """
    with chemin.open(encoding="utf-8-sig", newline="") as fichier:
        lecteur = csv.DictReader(fichier, delimiter=";")
        colonnes = lecteur.fieldnames or []
        manquantes = [colonne for colonne in COLONNES if colonne not in colonnes]
        if manquantes:
            raise FichierInvalide(f"colonne manquante : {', '.join(manquantes)}")

        for numero, ligne in enumerate(lecteur, start=2):
            yield numero, ligne


def lire_releves(chemin: Path, machines: dict[str, dict]) -> tuple[list[Releve], list[ReleveInvalide]]:
    """Lit tout le fichier et renvoie (releves_valides, erreurs) sans s'arrêter sur une ligne invalide."""
    releves: list[Releve] = []
    erreurs: list[ReleveInvalide] = []
    for numero, ligne in lignes_csv(chemin):
        try:
            releves.append(parser_releve(ligne, numero, machines))
        except ReleveInvalide as erreur:
            erreurs.append(erreur)
    return releves, erreurs


def ecrire_rejets(erreurs: list[ReleveInvalide], chemin: Path) -> None:
    """Écrit les lignes rejetées dans un CSV ligne;champ;message, lisible par Excel."""
    with chemin.open("w", encoding="utf-8-sig", newline="") as fichier:
        redacteur = csv.writer(fichier, delimiter=";")
        redacteur.writerow(("ligne", "champ", "message"))
        for erreur in erreurs:
            redacteur.writerow((erreur.ligne, erreur.champ, erreur.message))


# --- Étape 5 : analyser (dictionnaires, compréhensions) ------------------------------------------
def par_machine(releves: list[Releve]) -> dict[str, list[Releve]]:
    """Regroupe les relevés par machine : {machine: [relevés dans l'ordre du fichier]}."""
    groupes: dict[str, list[Releve]] = {}
    for releve in releves:
        groupes.setdefault(releve[0], []).append(releve)
    return groupes


def moyennes(releves: list[Releve]) -> dict[str, tuple[float, float]]:
    """{machine: (température moyenne arrondie à 0,1 ; vibration moyenne arrondie à 0,01)}."""
    groupes = par_machine(releves)
    return {
        machine: (
            round(math.fsum(releve[2] for releve in groupe) / len(groupe), 1),
            round(math.fsum(releve[3] for releve in groupe) / len(groupe), 2),
        )
        for machine, groupe in groupes.items()
    }


def alertes(releves: list[Releve], machines: dict[str, dict]) -> list[tuple[str, datetime, str]]:
    """Liste des dépassements de seuil (machine, horodatage, motif), triée par horodatage puis machine."""
    resultat: list[tuple[str, datetime, str]] = []
    for machine, horodatage, temperature, vibration in releves:
        fiche = machines[machine]
        if temperature > fiche["temp_max"]:
            resultat.append((
                machine, horodatage,
                f"température {temperature} > {fiche['temp_max']}",
            ))
        if vibration > fiche["vib_max"]:
            resultat.append((
                machine, horodatage,
                f"vibration {vibration} > {fiche['vib_max']}",
            ))
    return sorted(resultat, key=lambda alerte: (alerte[1], alerte[0]))


def machines_silencieuses(releves: list[Releve], machines: dict[str, dict]) -> set[str]:
    """Les machines du référentiel qui n'ont envoyé aucun relevé valide (capteur en panne ?)."""
    machines_vues = {releve[0] for releve in releves}
    return set(machines) - machines_vues


# --- Étape 6 : trier ---------------------------------------------------------------------------
def classement(releves: list[Releve], machines: dict[str, dict]) -> list[tuple[str, int]]:
    """[(machine, nombre d'alertes)] pour TOUTES les machines, la plus alertée d'abord, puis par identifiant."""
    nombres = {machine: 0 for machine in machines}
    for machine, _, _ in alertes(releves, machines):
        nombres[machine] += 1
    return sorted(nombres.items(), key=lambda element: (-element[1], element[0]))


def top_vibrations(releves: list[Releve], n: int = 3) -> list[Releve]:
    """Les n relevés de plus forte vibration, du plus fort au plus faible."""
    return sorted(releves, key=lambda releve: releve[3], reverse=True)[:max(0, n)]


# --- Étape 7 : le rapport JSON ------------------------------------------------------------------
def construire_rapport(releves: list[Releve], erreurs: list[ReleveInvalide], machines: dict[str, dict]) -> dict:
    """Le rapport sous forme d'un dictionnaire prêt pour json.dump (pas de datetime, pas d'ensemble)."""
    return {
        "releves_valides": len(releves),
        "lignes_rejetees": len(erreurs),
        "moyennes": {
            machine: {"temperature": temperature, "vibration": vibration}
            for machine, (temperature, vibration) in moyennes(releves).items()
        },
        "alertes": [
            {"machine": machine, "heure": horodatage.strftime("%H:%M"), "motif": motif}
            for machine, horodatage, motif in alertes(releves, machines)
        ],
        "classement": [
            machine for machine, nombre in classement(releves, machines) if nombre > 0
        ],
        "machines_silencieuses": sorted(machines_silencieuses(releves, machines)),
    }


def ecrire_rapport(rapport: dict, chemin: Path) -> None:
    """Écrit le rapport en JSON indenté, accents conservés."""
    with chemin.open("w", encoding="utf-8") as fichier:
        json.dump(rapport, fichier, indent=2, ensure_ascii=False)
        fichier.write("\n")


# --- Étape 8 (à la maison) : le journal des événements (expressions régulières) ------------------
MOTIF_EVENEMENT = re.compile(
    r"^\d{4}-\d{2}-\d{2} "
    r"(?P<heure>\d{2}:\d{2}:\d{2}) "
    r"\[(?P<machine>M-\d{2})\] "
    r"(?P<type>[A-Z]+) "
    r"code=(?P<code>[A-Z]\d{2})"
    r"(?: duree=(?P<duree>\d+))?$"
)


def lire_evenements(chemin: Path):
    """Générateur : un dictionnaire par ligne valide du journal, les lignes mal formées sont sautées.

    Clés : machine, type, code, heure, duree (entier en minutes, ou None si absente).
    """
    with chemin.open(encoding="utf-8") as fichier:
        for ligne in fichier:
            correspondance = MOTIF_EVENEMENT.fullmatch(ligne.rstrip("\r\n"))
            if correspondance is None:
                continue
            groupes = correspondance.groupdict()
            yield {
                "machine": groupes["machine"],
                "type": groupes["type"],
                "code": groupes["code"],
                "heure": groupes["heure"],
                "duree": int(groupes["duree"]) if groupes["duree"] is not None else None,
            }


def temps_arret(evenements) -> dict[str, int]:
    """Durée totale des ARRET par machine, en minutes."""
    durees: dict[str, int] = {}
    for evenement in evenements:
        if evenement["type"] == "ARRET" and evenement["duree"] is not None:
            machine = evenement["machine"]
            durees[machine] = durees.get(machine, 0) + evenement["duree"]
    return durees
