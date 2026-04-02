#!/usr/bin/env python3
"""
FilaStation Server v2.13 - mit Auftragsmanagement, .3mf Import & Abrechnung
NEU in v2.11:
  - Moonraker REST-API Integration (Port 7125) statt Port 4408
  - Neuer Tab "Druckhistorie" mit Jobs von Moonraker /server/history/list
  - Filamentverbrauch mm → Gramm Umrechnung pro Material/Dichte
  - Spulen-Zuordnung zur Druckhistorie (manuell per Klick)
  - Drucker-Status via Moonraker /server/info statt einfachem Ping
  - Settings: printer_api_port (7125) + printer_fluidd_port (4408) separat
"""

import sqlite3, logging, threading, time, zipfile, xml.etree.ElementTree as ET, base64, json, os
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify

DB_PATH      = Path.home() / "filamentserver" / "filament.db"
LOG_FILE     = Path.home() / "filamentserver" / "server.log"
UPLOAD_DIR   = Path.home() / "filamentserver" / "uploads"
THUMBNAIL_DIR = Path.home() / "filamentserver" / "thumbnails"
PORT, HOST = 5000, "0.0.0.0"

# Verzeichnisse anlegen
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger(__name__)
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB Upload-Limit

# COMMAND QUEUE für Master-Befehle
command_queue = {
    "command": None,
    "timestamp": None,
    "lock": threading.Lock()
}

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ── G-CODE PARSER ────────────────────────────────────────────────────────────
def parse_gcode(filepath_or_text, is_text=False):
    """
    Liest einen Creality Print G-Code und extrahiert:
    - Druckzeit aus '; estimated printing time (normal mode) = 3h 9m 51s'
    - Filamentverbrauch: Creality schreibt mm pro Extruder
      '; filament used [mm] = 813.80, 25090.30, 0.00, 0.00'
      Umrechnung mm -> g: mm x pi x (d/2)^2 x dichte / 1000
    """
    import math, re
    result = {"print_time_h": 0.0, "total_weight_g": 0.0, "filaments": []}
    try:
        if is_text:
            head = filepath_or_text[:50000] + filepath_or_text[-50000:]
        else:
            with open(filepath_or_text, 'r', encoding='utf-8', errors='ignore') as f:
                raw = f.read()
            head = raw[:50000] + raw[-50000:]

        time_s     = 0.0
        densities  = []
        diameters  = []
        mm_per_ext = []

        for line in head.split('\n'):
            line = line.strip()
            lo   = line.lower()

            # ; estimated printing time (normal mode) = 3h 9m 51s
            m = re.search(r'estimated printing time[^=]*=\s*(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?', lo)
            if m and any(m.groups()):
                t = int(m.group(1) or 0)*86400 + int(m.group(2) or 0)*3600 + int(m.group(3) or 0)*60 + int(m.group(4) or 0)
                if t > 0: time_s = t

            # ; filament_density: 1.27,1.27,1.25,1.25
            if 'filament_density' in lo:
                m = re.search(r'filament_density[:\s]+([0-9.,\s]+)', lo)
                if m: densities = [float(x.strip()) for x in m.group(1).split(',') if x.strip()]

            # ; filament_diameter: 1.75,1.75,1.75,1.75
            if 'filament_diameter' in lo:
                m = re.search(r'filament_diameter[:\s]+([0-9.,\s]+)', lo)
                if m: diameters = [float(x.strip()) for x in m.group(1).split(',') if x.strip()]

            # ; filament used [mm] = 813.80, 25090.30, 0.00, 0.00
            if 'filament used [mm]' in lo:
                m = re.search(r'filament used \[mm\]\s*=\s*([0-9.,\s]+)', lo)
                if m: mm_per_ext = [float(x.strip()) for x in m.group(1).split(',') if x.strip()]

            # Fallback: filament used [g] (andere Slicer)
            if 'filament used [g]' in lo and not mm_per_ext:
                m = re.search(r'filament used \[g\]\s*=\s*([\d.]+)', lo)
                if m: result["total_weight_g"] = float(m.group(1))

        if time_s > 0:
            result["print_time_h"] = round(time_s / 3600, 2)

        # mm -> Gramm umrechnen
        if mm_per_ext:
            total_g = 0.0
            for i, mm in enumerate(mm_per_ext):
                if mm <= 0: continue
                d   = diameters[i] if i < len(diameters) else 1.75
                rho = densities[i] if i < len(densities) else 1.24
                vol_cm3 = math.pi * (d/2/10)**2 * (mm/10)
                g = round(vol_cm3 * rho, 2)
                total_g += g
                result["filaments"].append({"color_index": i, "weight_g": g, "material": "", "length_mm": mm})
            if result["total_weight_g"] == 0:
                result["total_weight_g"] = round(total_g, 2)

    except Exception as e:
        log.warning(f"G-Code Parser Fehler: {e}")
    return result


# ── .3MF PARSER ──────────────────────────────────────────────────────────────
def parse_3mf(filepath):
    """
    Liest eine Creality Print .3mf Datei (Generisches 3MF Export).
    Extrahiert: Thumbnails pro Platte, Plattenanzahl, Filamenttypen.
    Gewicht + Zeit: NICHT in .3mf enthalten — kommen aus G-Code!
    Gibt dict zurück oder None bei Fehler.
    """
    import re, math
    result = {
        "print_time_h": 0.0,
        "total_weight_g": 0.0,
        "filaments": [],   # [{color_index, weight_g, material, color, density}]
        "plates": [],      # [{plate_id, print_time_h, weight_g, filaments, thumbnail_b64, name}]
        "thumbnail_b64": None,
        "filament_types": [],
        "filament_densities": [],
        "plate_count": 0,
        "has_slice_data": False
    }
    try:
        with zipfile.ZipFile(filepath, 'r') as z:
            names = z.namelist()

            # ── project_settings.config → Filamenttypen + Dichten ────────────
            if 'Metadata/project_settings.config' in names:
                try:
                    proj = json.loads(z.read('Metadata/project_settings.config').decode('utf-8'))
                    result["filament_types"]     = proj.get('filament_type', [])
                    result["filament_densities"] = [float(d) for d in proj.get('filament_density', [])]
                except Exception as e:
                    log.warning(f".3mf project_settings: {e}")

            # ── slice_info.config → PRO PLATTE: Zeit + Gewicht + Filamente ───
            # Nur im "Exportiere alle geslicten Druckplatten" Export vorhanden
            if 'Metadata/slice_info.config' in names:
                try:
                    si = z.read('Metadata/slice_info.config').decode('utf-8')
                    root = ET.fromstring(si)
                    result["has_slice_data"] = True

                    total_time_s   = 0.0
                    total_weight_g = 0.0

                    for plate_elem in root.findall('plate'):
                        # Metadaten ohne XPath auslesen (robuster)
                        meta = {}
                        for m in plate_elem.findall('metadata'):
                            meta[m.get('key', '')] = m.get('value', '')

                        pid    = int(meta.get('index', 1))
                        time_s = float(meta.get('prediction', 0))
                        weight = float(meta.get('weight', 0))

                        total_time_s   += time_s
                        total_weight_g += weight

                        # Per-Filament Daten dieser Platte
                        plate_filaments = []
                        for fil in plate_elem.findall('filament'):
                            plate_filaments.append({
                                "color_index": int(fil.get('id', 1)) - 1,
                                "weight_g":    float(fil.get('used_g', 0)),
                                "material":    fil.get('type', ''),
                                "color":       fil.get('color', ''),
                                "length_m":    float(fil.get('used_m', 0))
                            })

                        result["plates"].append({
                            "plate_id":      pid,
                            "print_time_h":  round(time_s / 3600, 2),
                            "weight_g":      weight,
                            "filaments":     plate_filaments,
                            "thumbnail_b64": None,
                            "name":          f"Platte {pid}"
                        })

                    result["print_time_h"]  = round(total_time_s / 3600, 2)
                    result["total_weight_g"] = round(total_weight_g, 2)

                    # Filament-Gesamtliste über alle Platten
                    fil_totals = {}
                    for plate in result["plates"]:
                        for f in plate["filaments"]:
                            idx = f["color_index"]
                            if idx not in fil_totals:
                                fil_totals[idx] = {"color_index": idx, "weight_g": 0.0,
                                                   "material": f["material"], "color": f["color"]}
                            fil_totals[idx]["weight_g"] += f["weight_g"]
                    result["filaments"] = [fil_totals[k] for k in sorted(fil_totals)]

                except Exception as e:
                    log.warning(f".3mf slice_info parse: {e}")

            # ── Thumbnails pro Platte ─────────────────────────────────────────
            plate_nums = set()
            for n in names:
                m = re.match(r'Metadata/plate_(\d+)\.png$', n)
                if m:
                    plate_nums.add(int(m.group(1)))

            if not result["plates"]:
                # Kein slice_info → nur Plattenstruktur aus Thumbnails
                result["plate_count"] = len(plate_nums)
                for pid in sorted(plate_nums):
                    result["plates"].append({
                        "plate_id": pid, "print_time_h": 0.0, "weight_g": 0.0,
                        "filaments": [], "thumbnail_b64": None, "name": f"Platte {pid}"
                    })
            else:
                result["plate_count"] = len(result["plates"])

            # Thumbnails den Platten zuordnen
            for plate in result["plates"]:
                pid = plate["plate_id"]
                for tname in [f'Metadata/plate_{pid}_small.png', f'Metadata/plate_{pid}.png']:
                    if tname in names:
                        try:
                            plate["thumbnail_b64"] = base64.b64encode(z.read(tname)).decode('utf-8')
                        except: pass
                        break

            # Erstes Thumbnail als Gesamt-Preview
            for plate in result["plates"]:
                if plate.get("thumbnail_b64"):
                    result["thumbnail_b64"] = plate["thumbnail_b64"]
                    break

    except Exception as e:
        log.warning(f".3mf Parser Fehler: {e}")
        return None

    log.info(f".3mf geparst: {result['plate_count']} Platten, {result['print_time_h']}h, "
             f"{result['total_weight_g']}g, slice_data={result['has_slice_data']}")
    return result

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        # Bestehende Tabellen
        conn.execute("""CREATE TABLE IF NOT EXISTS spools (
            uid TEXT PRIMARY KEY,
            material TEXT NOT NULL DEFAULT 'PLA',
            color TEXT NOT NULL DEFAULT 'UNBEKANNT',
            brand TEXT DEFAULT '',
            price_per_kg REAL NOT NULL DEFAULT 20.0,
            empty_weight INTEGER NOT NULL DEFAULT 220,
            full_weight INTEGER NOT NULL DEFAULT 1220,
            bed_temp INTEGER DEFAULT 60,
            nozzle_temp INTEGER DEFAULT 210,
            last_weight REAL,
            nfc_synced INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')))""")
        
        conn.execute("""CREATE TABLE IF NOT EXISTS weight_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL,
            weight REAL NOT NULL,
            logged_at TEXT DEFAULT (datetime('now')))""")
        
        conn.execute("""CREATE TABLE IF NOT EXISTS pending_spools (
            uid TEXT PRIMARY KEY,
            last_weight REAL,
            seen_at TEXT DEFAULT (datetime('now')))""")
        
        # Kostenrechner-Einstellungen
        conn.execute("""CREATE TABLE IF NOT EXISTS cost_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            power_cost_per_kwh REAL DEFAULT 0.35,
            printer_power_watts REAL DEFAULT 150.0,
            printer_purchase_price REAL DEFAULT 300.0,
            printer_lifetime_hours REAL DEFAULT 5000.0,
            failure_rate_percent REAL DEFAULT 5.0,
            default_profit_margin REAL DEFAULT 30.0,
            labor_cost_per_hour REAL DEFAULT 0.0,
            printer_labor_cost_per_hour REAL DEFAULT 0.0,
            pre_post_labor_cost_per_hour REAL DEFAULT 0.0,
            pre_post_time_minutes REAL DEFAULT 0.0,
            updated_at TEXT DEFAULT (datetime('now')))""")
        
        # ERWEITERTE Kalkulationen mit Slicer-Tracking
        conn.execute("""CREATE TABLE IF NOT EXISTS calculations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            material_uid TEXT,
            
            -- Slicer-Werte (Schätzung)
            slicer_weight_grams REAL NOT NULL,
            slicer_time_hours REAL NOT NULL,
            
            -- Tatsächliche Werte (nachgetragen)
            actual_weight_grams REAL,
            actual_time_hours REAL,
            
            -- Berechnete Deltas (automatisch)
            weight_delta_grams REAL,
            weight_delta_percent REAL,
            time_delta_hours REAL,
            time_delta_percent REAL,
            
            -- Kosten (basierend auf Slicer)
            material_cost REAL,
            power_cost REAL,
            wear_cost REAL,
            labor_cost REAL,
            failure_cost REAL,
            total_cost REAL,
            
            -- Verkauf
            profit_margin REAL,
            selling_price REAL,
            
            -- Tatsächliche Kosten (wenn nachgetragen)
            actual_total_cost REAL,
            cost_difference REAL,
            
            -- Status
            status TEXT DEFAULT 'planned',
            print_date TEXT,
            completed_at TEXT,
            
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (material_uid) REFERENCES spools(uid))""")
        
        # Material-Standardpreise
        conn.execute("""CREATE TABLE IF NOT EXISTS material_prices (
            material TEXT PRIMARY KEY,
            price_per_kg REAL NOT NULL,
            bed_temp INTEGER DEFAULT 60,
            nozzle_temp INTEGER DEFAULT 210,
            updated_at TEXT DEFAULT (datetime('now')))""")
        
        # NEU: Slicer-Korrekturfaktoren
        conn.execute("""CREATE TABLE IF NOT EXISTS correction_factors (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            weight_factor REAL DEFAULT 1.0,
            time_factor REAL DEFAULT 1.0,
            cost_factor REAL DEFAULT 1.0,
            last_calculated TEXT,
            samples_count INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now')))""")
        # Migration: cost_factor für bestehende DBs
        try: conn.execute("ALTER TABLE correction_factors ADD COLUMN cost_factor REAL DEFAULT 1.0")
        except: pass

        # ── NEU v2.13: Kundendatenbank ───────────────────────────────────────────
        conn.execute("""CREATE TABLE IF NOT EXISTS customers (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT NOT NULL,
            street  TEXT DEFAULT '',
            city    TEXT DEFAULT '',
            email   TEXT DEFAULT '',
            phone   TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        )""")
        # ── NEU v2.12: Aufträge ──────────────────────────────────────────────
        conn.execute("""CREATE TABLE IF NOT EXISTS job_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'Angebot',
            created_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            thumbnail_path TEXT DEFAULT '',
            offer_price REAL DEFAULT 0,
            actual_price REAL DEFAULT 0,
            profit_margin REAL DEFAULT 30,
            notes TEXT DEFAULT '')""")

        # ── NEU v2.12: Druckplatten pro Auftrag ─────────────────────────────
        conn.execute("""CREATE TABLE IF NOT EXISTS job_plates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            plate_number INTEGER DEFAULT 1,
            status TEXT DEFAULT 'Offen',
            slicer_weight_g REAL DEFAULT 0,
            slicer_time_h REAL DEFAULT 0,
            actual_weight_g REAL DEFAULT 0,
            actual_time_h REAL DEFAULT 0,
            pre_post_time_min REAL DEFAULT 0,
            profit_margin REAL DEFAULT 30,
            moonraker_job_id TEXT DEFAULT '',
            filaments TEXT DEFAULT '[]',
            offer_total_cost REAL DEFAULT 0,
            offer_selling_price REAL DEFAULT 0,
            actual_total_cost REAL DEFAULT 0,
            actual_selling_price REAL DEFAULT 0,
            failure_notes TEXT DEFAULT '',
            include_in_costs INTEGER DEFAULT 1,
            thumbnail_path TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            FOREIGN KEY (order_id) REFERENCES job_orders(id))""")
        # Migration: thumbnail_path falls noch nicht vorhanden
        try:
            conn.execute("ALTER TABLE job_plates ADD COLUMN thumbnail_path TEXT DEFAULT ''")
            conn.commit()
        except: pass
        
        # Brands-Tabelle (dynamisches Hersteller-Dropdown)
        conn.execute("""CREATE TABLE IF NOT EXISTS brands (
            name TEXT PRIMARY KEY,
            created_at TEXT DEFAULT (datetime('now')))""")
        
        # Vorausgefüllte Standard-Hersteller
        default_brands = ['3DTRCEK', 'Bambu Lab', 'Polymaker', 'Prusament', 'SUNLU', 'Eryone', 'Hatchbox', 'eSUN', 'Overture', 'Filamentworld']
        for b in default_brands:
            conn.execute("INSERT OR IGNORE INTO brands (name) VALUES (?)", (b,))

        # Migrations
        alter_commands = [
            "ALTER TABLE spools ADD COLUMN price_per_kg REAL NOT NULL DEFAULT 20.0",
            "ALTER TABLE spools ADD COLUMN brand TEXT DEFAULT ''",
            "ALTER TABLE spools ADD COLUMN bed_temp INTEGER DEFAULT 60",
            "ALTER TABLE spools ADD COLUMN nozzle_temp INTEGER DEFAULT 210",
            "ALTER TABLE spools ADD COLUMN nfc_synced INTEGER DEFAULT 0",
            "ALTER TABLE spools ADD COLUMN created_at TEXT DEFAULT (datetime('now'))",
            "ALTER TABLE spools ADD COLUMN updated_at TEXT DEFAULT (datetime('now'))",
            "ALTER TABLE spools ADD COLUMN storage_location TEXT DEFAULT ''",
            "ALTER TABLE spools ADD COLUMN order_number TEXT DEFAULT ''",
            "ALTER TABLE spools ADD COLUMN brand_color TEXT DEFAULT ''",
            "ALTER TABLE spools ADD COLUMN display_color TEXT DEFAULT ''",
            "ALTER TABLE spools ADD COLUMN notes TEXT DEFAULT ''"
        ]
        
        for cmd in alter_commands:
            try:
                conn.execute(cmd)
                log.info(f"Migration: {cmd.split('ADD')[1] if 'ADD' in cmd else cmd}")
            except sqlite3.OperationalError:
                pass
        
        # Migration: material_prices Temperaturen
        for cmd in [
            "ALTER TABLE material_prices ADD COLUMN bed_temp INTEGER DEFAULT 60",
            "ALTER TABLE material_prices ADD COLUMN nozzle_temp INTEGER DEFAULT 210",
        ]:
            try:
                conn.execute(cmd)
            except sqlite3.OperationalError:
                pass
        
        # Standard-Einstellungen
        conn.execute("""INSERT OR IGNORE INTO cost_settings (id) VALUES (1)""")
        for cmd in [
            "ALTER TABLE cost_settings ADD COLUMN printer_labor_cost_per_hour REAL DEFAULT 0.0",
            "ALTER TABLE cost_settings ADD COLUMN pre_post_labor_cost_per_hour REAL DEFAULT 0.0",
            "ALTER TABLE cost_settings ADD COLUMN pre_post_time_minutes REAL DEFAULT 0.0",
        ]:
            try:
                conn.execute(cmd)
            except sqlite3.OperationalError:
                pass
        conn.execute("""INSERT OR IGNORE INTO correction_factors (id) VALUES (1)""")
        
        # Standard-Materialpreise (mat, price, bed_temp, nozzle_temp)
        default_prices = [
            ('PLA',      20.0,  60, 210),
            ('PETG',     25.0,  70, 235),
            ('ABS',      22.0, 100, 240),
            ('ASA',      30.0, 100, 250),
            ('TPU',      35.0,  40, 220),
            ('NYLON',    40.0,  70, 250),
            ('PLA+',     22.0,  60, 215),
            ('PETG-CF',  35.0,  70, 240),
            ('ABS-CF',   38.0, 100, 245),
            ('PC',       45.0, 110, 270),
            ('HIPS',     25.0, 100, 230),
            ('PVA',      50.0,  45, 195),
        ]
        for mat, price, bed, nozzle in default_prices:
            conn.execute("""INSERT OR IGNORE INTO material_prices (material, price_per_kg, bed_temp, nozzle_temp) 
                VALUES (?, ?, ?, ?)""", (mat, price, bed, nozzle))
        
        # Neue Tabelle: Netzwerk-Einstellungen (v2.10+)
        conn.execute("""CREATE TABLE IF NOT EXISTS network_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            printer_ip TEXT DEFAULT '192.168.178.57',
            printer_api_port INTEGER DEFAULT 7125,
            printer_fluidd_port INTEGER DEFAULT 4408,
            waage_ip TEXT DEFAULT '192.168.178.65')""")
        conn.execute("INSERT OR IGNORE INTO network_settings (id) VALUES (1)")
        for cmd in [
            "ALTER TABLE network_settings ADD COLUMN printer_api_port INTEGER DEFAULT 7125",
            "ALTER TABLE network_settings ADD COLUMN printer_fluidd_port INTEGER DEFAULT 4408",
        ]:
            try: conn.execute(cmd)
            except sqlite3.OperationalError: pass

        # Firmendaten für PDF-Export
        conn.execute("""CREATE TABLE IF NOT EXISTS company_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            name TEXT DEFAULT '',
            street TEXT DEFAULT '',
            city TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            website TEXT DEFAULT '',
            tax_id TEXT DEFAULT '',
            bank TEXT DEFAULT '',
            iban TEXT DEFAULT '',
            logo_path TEXT DEFAULT '')""")
        conn.execute("INSERT OR IGNORE INTO company_settings (id) VALUES (1)")

        # Kundenfelder in job_orders (Migration)
        for cmd in [
            "ALTER TABLE job_orders ADD COLUMN customer_name TEXT DEFAULT ''",
            "ALTER TABLE job_orders ADD COLUMN customer_street TEXT DEFAULT ''",
            "ALTER TABLE job_orders ADD COLUMN customer_city TEXT DEFAULT ''",
            "ALTER TABLE job_orders ADD COLUMN customer_email TEXT DEFAULT ''",
            "ALTER TABLE job_orders ADD COLUMN customer_phone TEXT DEFAULT ''",
        ]:
            try: conn.execute(cmd)
            except sqlite3.OperationalError: pass

        # Neue Tabelle: Druckhistorie (Moonraker-Jobs lokal gecacht)
        conn.execute("""CREATE TABLE IF NOT EXISTS print_history (
            job_id TEXT PRIMARY KEY,
            filename TEXT,
            status TEXT,
            start_time REAL,
            end_time REAL,
            print_duration REAL,
            filament_used_mm REAL,
            filament_used_g REAL,
            spool_uid TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            synced_at TEXT DEFAULT (datetime('now')))""")

        conn.commit()
    log.info(f"DB: {DB_PATH}")

# ========== NETZWERK-HILFSFUNKTIONEN ==========

def get_network_settings():
    """Lese Netzwerk-Einstellungen aus DB — robust gegen fehlende Tabelle"""
    defaults = {
        "printer_ip": "192.168.178.57",
        "printer_api_port": 7125,
        "printer_fluidd_port": 4408,
        "waage_ip": "192.168.178.65"
    }
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM network_settings WHERE id=1").fetchone()
            if row:
                d = dict(row)
                # Fehlende Spalten mit Defaults auffüllen
                for k, v in defaults.items():
                    if k not in d or d[k] is None:
                        d[k] = v
                return d
    except Exception:
        pass
    return defaults

# Materialdichten für mm→g Umrechnung (g/cm³), Filament 1.75mm
MATERIAL_DENSITY = {
    "PLA":     1.24,
    "PLA+":    1.24,
    "PETG":    1.27,
    "PETG-CF": 1.30,
    "ABS":     1.05,
    "ABS-CF":  1.10,
    "ASA":     1.07,
    "TPU":     1.20,
    "NYLON":   1.14,
    "PC":      1.20,
    "HIPS":    1.04,
    "PVA":     1.23,
}

def filament_mm_to_g(mm, material="PLA", diameter=1.75):
    """Rechne Filament mm → Gramm um"""
    density = MATERIAL_DENSITY.get(material.upper(), 1.24)
    radius_cm = (diameter / 2) / 10  # mm → cm
    import math
    volume_cm3 = math.pi * radius_cm**2 * (mm / 10)  # mm → cm
    return round(volume_cm3 * density, 2)

def row_to_dict(row):
    d = dict(row)
    ew, fw, lw = d.get("empty_weight",220), d.get("full_weight",1220), d.get("last_weight")
    if lw is not None and fw > ew:
        rg = max(0, lw - ew)
        d["remaining_grams"]   = round(rg, 1)
        d["remaining_percent"] = round(min(100, rg / (fw - ew) * 100), 1)
    else:
        d["remaining_grams"] = None
        d["remaining_percent"] = None
    return d

def calculate_correction_factors():
    """Berechne Korrekturfaktoren aus abgeschlossenen Drucken"""
    with get_db() as conn:
        # Hole alle abgeschlossenen Drucke mit tatsächlichen Werten
        rows = conn.execute("""SELECT 
            slicer_weight_grams, actual_weight_grams,
            slicer_time_hours, actual_time_hours
            FROM calculations 
            WHERE status='completed' 
            AND actual_weight_grams IS NOT NULL 
            AND actual_time_hours IS NOT NULL""").fetchall()
        
        if len(rows) < 3:  # Mindestens 3 Drucke für aussagekräftige Statistik
            return None
        
        # Durchschnittliche Abweichungen berechnen
        weight_ratios = [r['actual_weight_grams'] / r['slicer_weight_grams'] for r in rows if r['slicer_weight_grams'] > 0]
        time_ratios = [r['actual_time_hours'] / r['slicer_time_hours'] for r in rows if r['slicer_time_hours'] > 0]
        
        if not weight_ratios or not time_ratios:
            return None
        
        avg_weight_factor = sum(weight_ratios) / len(weight_ratios)
        avg_time_factor = sum(time_ratios) / len(time_ratios)
        
        # In DB speichern
        conn.execute("""UPDATE correction_factors SET
            weight_factor = ?,
            time_factor = ?,
            samples_count = ?,
            last_calculated = datetime('now'),
            updated_at = datetime('now')
            WHERE id = 1""", (avg_weight_factor, avg_time_factor, len(rows)))
        conn.commit()
        
        return {
            'weight_factor': round(avg_weight_factor, 3),
            'time_factor': round(avg_time_factor, 3),
            'samples': len(rows)
        }

# ========== KOSTENRECHNER API ==========

@app.route("/api/cost/settings")
def get_cost_settings():
    with get_db() as conn:
        row = conn.execute("SELECT * FROM cost_settings WHERE id=1").fetchone()
        return jsonify(dict(row) if row else {})

@app.route("/api/cost/settings", methods=["POST"])
def update_cost_settings():
    data = request.get_json(force=True)
    
    with get_db() as conn:
        conn.execute("""UPDATE cost_settings SET
            power_cost_per_kwh = ?,
            printer_power_watts = ?,
            printer_purchase_price = ?,
            printer_lifetime_hours = ?,
            failure_rate_percent = ?,
            default_profit_margin = ?,
            labor_cost_per_hour = ?,
            printer_labor_cost_per_hour = ?,
            pre_post_labor_cost_per_hour = ?,
            pre_post_time_minutes = ?,
            updated_at = datetime('now')
            WHERE id = 1""",
            (data.get('power_cost_per_kwh', 0.35),
             data.get('printer_power_watts', 150.0),
             data.get('printer_purchase_price', 300.0),
             data.get('printer_lifetime_hours', 5000.0),
             data.get('failure_rate_percent', 5.0),
             data.get('default_profit_margin', 30.0),
             data.get('labor_cost_per_hour', 0.0),
             data.get('printer_labor_cost_per_hour', 0.0),
             data.get('pre_post_labor_cost_per_hour', 0.0),
             data.get('pre_post_time_minutes', 0.0)))
        conn.commit()
    
    log.info("Kostenrechner-Einstellungen aktualisiert")
    return jsonify({"status": "ok"})

@app.route("/api/cost/correction-factors")
def get_correction_factors():
    """Hole aktuelle Korrekturfaktoren"""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM correction_factors WHERE id=1").fetchone()
        if row:
            return jsonify(dict(row))
        return jsonify({"weight_factor": 1.0, "time_factor": 1.0, "samples_count": 0})

@app.route("/api/cost/calculate-multi", methods=["POST"])
def calculate_cost_multi():
    """Berechne Druckkosten mit mehreren Rollen (CFS Multi-Color)"""
    data = request.get_json(force=True)
    rolls = data.get('rolls', [])
    slicer_time = float(data.get('slicer_time_hours', 0))
    use_correction = data.get('use_correction', False)

    with get_db() as conn:
        settings = dict(conn.execute("SELECT * FROM cost_settings WHERE id=1").fetchone())

        # Korrekturfaktor
        time_factor = 1.0
        if use_correction:
            cf = dict(conn.execute("SELECT * FROM correction_factors WHERE id=1").fetchone())
            if cf['samples_count'] >= 3:
                time_factor = cf['time_factor']

        print_hours = slicer_time * time_factor

        # Materialkosten: pro Rolle einzeln berechnen
        material_cost = 0
        rolls_detail = []
        total_weight = 0
        for roll in rolls:
            uid = roll.get('uid', '').upper().strip()
            weight_g = float(roll.get('weight_grams', 0))
            pct = float(roll.get('percent', 0))
            if use_correction:
                cf = dict(conn.execute("SELECT * FROM correction_factors WHERE id=1").fetchone())
                if cf['samples_count'] >= 3:
                    weight_g = weight_g * cf.get('weight_factor', 1.0)

            spool = conn.execute("SELECT price_per_kg, material, color FROM spools WHERE uid=?", (uid,)).fetchone()
            if spool:
                roll_cost = (weight_g / 1000.0) * spool['price_per_kg']
                material_cost += roll_cost
                total_weight += weight_g
                rolls_detail.append({
                    'uid': uid,
                    'material': spool['material'],
                    'color': spool['color'],
                    'weight_grams': round(weight_g, 1),
                    'percent': pct,
                    'material_cost': round(roll_cost, 2)
                })

        # Strom, Verschleiß, Arbeit – einmal für Gesamtdruck
        power_kwh = (settings['printer_power_watts'] / 1000.0) * print_hours
        power_cost = power_kwh * settings['power_cost_per_kwh']
        wear_cost = (settings['printer_purchase_price'] / settings['printer_lifetime_hours']) * print_hours
        printer_labor = settings.get('printer_labor_cost_per_hour', 0.0) * print_hours
        pre_post_minutes = float(data.get('pre_post_time_minutes') or settings.get('pre_post_time_minutes', 0.0))
        pre_post_labor = settings.get('pre_post_labor_cost_per_hour', 0.0) * (pre_post_minutes / 60.0)
        labor_cost = printer_labor + pre_post_labor

        subtotal = material_cost + power_cost + wear_cost + labor_cost
        failure_cost = subtotal * (settings['failure_rate_percent'] / 100.0)
        total_cost = subtotal + failure_cost

        profit_margin = float(data.get('profit_margin', settings['default_profit_margin']))
        selling_price = total_cost / (1 - profit_margin / 100.0) if profit_margin < 100 else total_cost

        return jsonify({
            "material_cost": round(material_cost, 2),
            "power_cost": round(power_cost, 2),
            "wear_cost": round(wear_cost, 2),
            "labor_cost": round(labor_cost, 2),
            "printer_labor_cost": round(printer_labor, 2),
            "pre_post_labor_cost": round(pre_post_labor, 2),
            "failure_cost": round(failure_cost, 2),
            "total_cost": round(total_cost, 2),
            "profit_margin": profit_margin,
            "selling_price": round(selling_price, 2),
            "profit_amount": round(selling_price - total_cost, 2),
            "power_kwh": round(power_kwh, 3),
            "total_weight_grams": round(total_weight, 1),
            "rolls_detail": rolls_detail,
            "corrected_weight": round(total_weight, 1) if use_correction else None,
            "corrected_time": round(print_hours, 2) if use_correction else None
        })

@app.route("/api/cost/calculate", methods=["POST"])
def calculate_cost():
    """Berechne Druckkosten (mit optionalen Korrekturfaktoren)"""
    data = request.get_json(force=True)
    
    slicer_weight = float(data.get('slicer_weight_grams', 0))
    slicer_time = float(data.get('slicer_time_hours', 0))
    material_uid = data.get('material_uid')
    use_correction = data.get('use_correction', False)
    
    # Optional: Korrekturfaktoren anwenden
    if use_correction:
        with get_db() as conn:
            cf = dict(conn.execute("SELECT * FROM correction_factors WHERE id=1").fetchone())
            if cf['samples_count'] >= 3:
                weight_g = slicer_weight * cf['weight_factor']
                print_hours = slicer_time * cf['time_factor']
            else:
                weight_g = slicer_weight
                print_hours = slicer_time
    else:
        weight_g = slicer_weight
        print_hours = slicer_time
    
    with get_db() as conn:
        # Hole Einstellungen
        settings = dict(conn.execute("SELECT * FROM cost_settings WHERE id=1").fetchone())
        
        # Materialkosten
        material_cost = 0
        if material_uid:
            spool = conn.execute("SELECT price_per_kg, material FROM spools WHERE uid=?", 
                               (material_uid,)).fetchone()
            if spool and spool['price_per_kg']:
                material_cost = (weight_g / 1000.0) * spool['price_per_kg']
        else:
            price_per_kg = float(data.get('price_per_kg', 20.0))
            material_cost = (weight_g / 1000.0) * price_per_kg
        
        # Stromkosten
        power_kwh = (settings['printer_power_watts'] / 1000.0) * print_hours
        power_cost = power_kwh * settings['power_cost_per_kwh']
        
        # Verschleißkosten
        wear_cost = (settings['printer_purchase_price'] / settings['printer_lifetime_hours']) * print_hours
        
        # Arbeitskosten Drucker (läuft während des Drucks)
        printer_labor = settings.get('printer_labor_cost_per_hour', 0.0) * print_hours
        
        # Arbeitskosten Vor- und Nachbearbeitung
        # Request kann Minuten überschreiben, sonst Einstellung verwenden
        pre_post_minutes = data.get('pre_post_time_minutes')
        if pre_post_minutes is None:
            pre_post_minutes = settings.get('pre_post_time_minutes', 0.0)
        pre_post_minutes = float(pre_post_minutes)
        pre_post_labor = settings.get('pre_post_labor_cost_per_hour', 0.0) * (pre_post_minutes / 60.0)
        
        labor_cost = printer_labor + pre_post_labor
        
        # Zwischensumme
        subtotal = material_cost + power_cost + wear_cost + labor_cost
        
        # Fehldruckkosten
        failure_cost = subtotal * (settings['failure_rate_percent'] / 100.0)
        
        # Gesamtkosten
        total_cost = subtotal + failure_cost
        
        # Verkaufspreis
        profit_margin = float(data.get('profit_margin', settings['default_profit_margin']))
        selling_price = total_cost / (1 - profit_margin / 100.0)
        
        result = {
            "material_cost": round(material_cost, 2),
            "power_cost": round(power_cost, 2),
            "wear_cost": round(wear_cost, 2),
            "labor_cost": round(labor_cost, 2),
            "printer_labor_cost": round(printer_labor, 2),
            "pre_post_labor_cost": round(pre_post_labor, 2),
            "failure_cost": round(failure_cost, 2),
            "total_cost": round(total_cost, 2),
            "profit_margin": profit_margin,
            "selling_price": round(selling_price, 2),
            "profit_amount": round(selling_price - total_cost, 2),
            "power_kwh": round(power_kwh, 3),
            "corrected_weight": round(weight_g, 1) if use_correction else None,
            "corrected_time": round(print_hours, 2) if use_correction else None
        }
        
        return jsonify(result)

@app.route("/api/cost/calculations")
def get_calculations():
    """Hole alle Kalkulationen mit Status"""
    with get_db() as conn:
        rows = conn.execute("""SELECT c.*, s.material, s.color, s.brand 
            FROM calculations c 
            LEFT JOIN spools s ON c.material_uid = s.uid 
            ORDER BY c.created_at DESC""").fetchall()
        return jsonify([dict(r) for r in rows])

@app.route("/api/cost/calculations", methods=["POST"])
def save_calculation():
    """Speichere neue Kalkulation"""
    data = request.get_json(force=True)
    
    with get_db() as conn:
        cursor = conn.execute("""INSERT INTO calculations 
            (name, description, material_uid, 
             slicer_weight_grams, slicer_time_hours,
             material_cost, power_cost, wear_cost, labor_cost, failure_cost,
             total_cost, profit_margin, selling_price,
             status, print_date)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data.get('name'),
             data.get('description'),
             data.get('material_uid'),
             data.get('slicer_weight_grams'),
             data.get('slicer_time_hours'),
             data.get('material_cost'),
             data.get('power_cost'),
             data.get('wear_cost'),
             data.get('labor_cost'),
             data.get('failure_cost'),
             data.get('total_cost'),
             data.get('profit_margin'),
             data.get('selling_price'),
             data.get('status', 'planned'),
             data.get('print_date')))
        conn.commit()
        
        calc_id = cursor.lastrowid
        log.info(f"Kalkulation gespeichert: {data.get('name')} (ID: {calc_id})")
        return jsonify({"status": "ok", "id": calc_id})

@app.route("/api/cost/calculations/<int:calc_id>", methods=["PUT"])
def update_calculation(calc_id):
    """Aktualisiere Kalkulation (z.B. tatsächliche Werte nachtragen)"""
    data = request.get_json(force=True)
    
    with get_db() as conn:
        # Hole aktuelle Kalkulation
        calc = dict(conn.execute("SELECT * FROM calculations WHERE id=?", (calc_id,)).fetchone())
        
        actual_weight = data.get('actual_weight_grams')
        actual_time = data.get('actual_time_hours')
        
        # Berechne Deltas wenn tatsächliche Werte vorhanden
        weight_delta = None
        weight_delta_pct = None
        time_delta = None
        time_delta_pct = None
        actual_total_cost = None
        cost_difference = None
        
        if actual_weight and calc['slicer_weight_grams']:
            weight_delta = actual_weight - calc['slicer_weight_grams']
            weight_delta_pct = (weight_delta / calc['slicer_weight_grams']) * 100
        
        if actual_time and calc['slicer_time_hours']:
            time_delta = actual_time - calc['slicer_time_hours']
            time_delta_pct = (time_delta / calc['slicer_time_hours']) * 100
        
        # Berechne tatsächliche Kosten wenn Werte vorhanden
        if actual_weight and actual_time:
            # Hole Einstellungen
            settings = dict(conn.execute("SELECT * FROM cost_settings WHERE id=1").fetchone())
            spool = conn.execute("SELECT price_per_kg FROM spools WHERE uid=?", 
                               (calc['material_uid'],)).fetchone()
            
            if spool:
                mat_cost = (actual_weight / 1000.0) * spool['price_per_kg']
                pwr_cost = (settings['printer_power_watts'] / 1000.0) * actual_time * settings['power_cost_per_kwh']
                wear = (settings['printer_purchase_price'] / settings['printer_lifetime_hours']) * actual_time
                labor = settings['labor_cost_per_hour'] * actual_time
                subtotal = mat_cost + pwr_cost + wear + labor
                failure = subtotal * (settings['failure_rate_percent'] / 100.0)
                actual_total_cost = subtotal + failure
                cost_difference = actual_total_cost - calc['total_cost']
        
        # Update Kalkulation
        conn.execute("""UPDATE calculations SET
            actual_weight_grams = ?,
            actual_time_hours = ?,
            weight_delta_grams = ?,
            weight_delta_percent = ?,
            time_delta_hours = ?,
            time_delta_percent = ?,
            actual_total_cost = ?,
            cost_difference = ?,
            status = ?,
            completed_at = ?
            WHERE id = ?""",
            (actual_weight,
             actual_time,
             weight_delta,
             weight_delta_pct,
             time_delta,
             time_delta_pct,
             actual_total_cost,
             cost_difference,
             data.get('status', calc['status']),
             datetime.now().isoformat() if data.get('status') == 'completed' else None,
             calc_id))
        conn.commit()
        
        # Korrekturfaktoren neu berechnen wenn Status = completed
        if data.get('status') == 'completed' and actual_weight and actual_time:
            factors = calculate_correction_factors()
            log.info(f"Korrekturfaktoren aktualisiert: {factors}")
            
            # ── NEU v2.11: Print-History-Eintrag aus Kalkulation anlegen ──
            # Filamentverbrauch in mm zurückrechnen (g → mm) für einheitliche Speicherung
            import math
            spool_material = "PLA"
            if calc.get('material_uid'):
                spool_row = conn.execute(
                    "SELECT material FROM spools WHERE uid=?",
                    (calc['material_uid'],)).fetchone()
                if spool_row:
                    spool_material = spool_row['material']
            density = MATERIAL_DENSITY.get(spool_material.upper(), 1.24)
            radius_cm = (1.75 / 2) / 10
            vol_per_mm = math.pi * radius_cm**2 * 0.1  # cm³ pro mm
            filament_mm = round((actual_weight / 1000.0) / (vol_per_mm * density) * 10, 1) \
                if vol_per_mm > 0 else 0

            job_id = f"calc_{calc_id}"
            print_date = calc.get('print_date') or datetime.now().strftime("%Y-%m-%d")
            # Unix-Timestamp aus Datum (Mitternacht)
            from datetime import datetime as dt
            try:
                start_ts = dt.strptime(print_date, "%Y-%m-%d").timestamp()
            except Exception:
                start_ts = dt.now().timestamp()
            end_ts = start_ts + actual_time * 3600

            conn.execute("""INSERT OR REPLACE INTO print_history
                (job_id, filename, status, start_time, end_time, print_duration,
                 filament_used_mm, filament_used_g, spool_uid, notes, synced_at)
                VALUES (?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (job_id,
                 calc.get('name', f"Kalkulation #{calc_id}"),
                 start_ts, end_ts,
                 actual_time * 3600,
                 filament_mm,
                 actual_weight,
                 calc.get('material_uid', ''),
                 calc.get('description', ''),
                ))
            conn.commit()
            log.info(f"Print-History: Eintrag {job_id} aus Kalkulation angelegt "
                     f"({actual_weight:.1f}g, {actual_time:.2f}h)")

        log.info(f"Kalkulation {calc_id} aktualisiert")
        return jsonify({"status": "ok"})

@app.route("/api/cost/calculations/<int:calc_id>", methods=["DELETE"])
def delete_calculation(calc_id):
    with get_db() as conn:
        conn.execute("DELETE FROM calculations WHERE id=?", (calc_id,))
        conn.commit()
    log.info(f"Kalkulation gelöscht: ID {calc_id}")
    return jsonify({"status": "ok"})

@app.route("/api/cost/material-prices")
def get_material_prices():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM material_prices ORDER BY material").fetchall()
        return jsonify([dict(r) for r in rows])

@app.route("/api/cost/material-prices", methods=["POST"])
def update_material_price():
    data = request.get_json(force=True)
    material = data.get('material')
    price = float(data.get('price_per_kg', 0))
    bed = int(data.get('bed_temp', 60))
    nozzle = int(data.get('nozzle_temp', 210))
    
    with get_db() as conn:
        conn.execute("""INSERT INTO material_prices (material, price_per_kg, bed_temp, nozzle_temp, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(material) DO UPDATE SET 
            price_per_kg=excluded.price_per_kg,
            bed_temp=excluded.bed_temp,
            nozzle_temp=excluded.nozzle_temp,
            updated_at=datetime('now')""", (material, price, bed, nozzle))
        conn.commit()
    
    log.info(f"Materialpreis aktualisiert: {material} = {price}€/kg Bed:{bed}° Nozzle:{nozzle}°")
    return jsonify({"status": "ok"})

@app.route("/api/cost/statistics")
def get_statistics():
    """Hole Gesamt-Statistiken über alle Drucke"""
    with get_db() as conn:
        stats = {}
        
        # Gesamt-Anzahl Kalkulationen
        stats['total_calculations'] = conn.execute("SELECT COUNT(*) as cnt FROM calculations").fetchone()['cnt']
        
        # Anzahl abgeschlossene Drucke
        stats['completed_prints'] = conn.execute(
            "SELECT COUNT(*) as cnt FROM calculations WHERE status='completed'").fetchone()['cnt']
        
        # Durchschnittliche Abweichungen (nur completed)
        avg_deltas = conn.execute("""SELECT 
            AVG(weight_delta_percent) as avg_weight_delta,
            AVG(time_delta_percent) as avg_time_delta,
            AVG(cost_difference) as avg_cost_diff
            FROM calculations 
            WHERE status='completed' 
            AND actual_weight_grams IS NOT NULL""").fetchone()
        
        if avg_deltas:
            stats['avg_weight_deviation_percent'] = round(avg_deltas['avg_weight_delta'] or 0, 1)
            stats['avg_time_deviation_percent'] = round(avg_deltas['avg_time_delta'] or 0, 1)
            stats['avg_cost_difference'] = round(avg_deltas['avg_cost_diff'] or 0, 2)
        
        # Korrekturfaktoren
        cf = dict(conn.execute("SELECT * FROM correction_factors WHERE id=1").fetchone())
        stats['correction_factors'] = {
            'weight': cf['weight_factor'],
            'time': cf['time_factor'],
            'samples': cf['samples_count'],
            'last_calculated': cf['last_calculated']
        }
        
        return jsonify(stats)

# ========== MASTER COMMAND API ==========

@app.route("/api/command/send", methods=["POST"])
def send_command():
    data = request.get_json(force=True)
    cmd = data.get("command", "").lower()
    
    valid_commands = ["tare", "calibrate", "reboot", "shutdown"]
    if cmd not in valid_commands:
        return jsonify({"status": "error", "message": f"Ungültiger Befehl. Erlaubt: {valid_commands}"}), 400
    
    with command_queue["lock"]:
        command_queue["command"] = cmd
        command_queue["timestamp"] = datetime.now().isoformat()
    
    log.info(f"📡 Command empfangen: {cmd}")
    return jsonify({"status": "ok", "command": cmd, "message": "Befehl in Queue"})

@app.route("/api/command/poll")
def poll_command():
    with command_queue["lock"]:
        cmd = command_queue["command"]
        ts = command_queue["timestamp"]
        command_queue["last_poll"] = datetime.now().isoformat()  # Heartbeat
        
        if cmd:
            command_queue["command"] = None
            command_queue["timestamp"] = None
            log.info(f"📥 Command abgeholt: {cmd}")
        
        return jsonify({"command": cmd, "timestamp": ts})

@app.route("/api/command/ack", methods=["POST"])
def acknowledge_command():
    data = request.get_json(force=True)
    cmd = data.get("command")
    status = data.get("status")
    msg = data.get("message", "")
    
    log.info(f"✅ Command ACK: {cmd} - {status} - {msg}")
    return jsonify({"status": "ok"})

@app.route("/api/printer/status")
def printer_status():
    """Creality K2+ Status via Moonraker API (Port 7125)"""
    import urllib.request, json as _json
    net = get_network_settings()
    printer_ip = net["printer_ip"]
    api_port   = net.get("printer_api_port", 7125)
    fluidd_port = net.get("printer_fluidd_port", 4408)
    fluidd_url = f"http://{printer_ip}:{fluidd_port}/#/"
    try:
        req = urllib.request.urlopen(
            f"http://{printer_ip}:{api_port}/server/info", timeout=2)
        info = _json.loads(req.read().decode())
        klipper_state = info.get("result", {}).get("klippy_state", "unknown")
        return jsonify({
            "online": True,
            "url": fluidd_url,
            "klippy_state": klipper_state,
            "moonraker": True
        })
    except Exception:
        return jsonify({"online": False, "url": fluidd_url, "moonraker": False})

@app.route("/api/network/settings")
def get_network():
    """Netzwerk-Einstellungen lesen"""
    return jsonify(get_network_settings())

@app.route("/api/network/settings", methods=["POST"])
def save_network():
    """Netzwerk-Einstellungen speichern"""
    data = request.get_json(force=True)
    printer_ip    = data.get("printer_ip", "192.168.178.57")
    api_port      = int(data.get("printer_api_port", 7125))
    fluidd_port   = int(data.get("printer_fluidd_port", 4408))
    waage_ip      = data.get("waage_ip", "192.168.178.65")
    with get_db() as conn:
        # Tabelle sicherheitshalber anlegen falls noch nicht vorhanden
        conn.execute("""CREATE TABLE IF NOT EXISTS network_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            printer_ip TEXT DEFAULT '192.168.178.57',
            printer_api_port INTEGER DEFAULT 7125,
            printer_fluidd_port INTEGER DEFAULT 4408,
            waage_ip TEXT DEFAULT '192.168.178.65')""")
        # Spalten ergänzen falls DB aus alter Version stammt
        for cmd in [
            "ALTER TABLE network_settings ADD COLUMN printer_api_port INTEGER DEFAULT 7125",
            "ALTER TABLE network_settings ADD COLUMN printer_fluidd_port INTEGER DEFAULT 4408",
        ]:
            try: conn.execute(cmd)
            except sqlite3.OperationalError: pass
        # Zeile sicherstellen
        conn.execute("INSERT OR IGNORE INTO network_settings (id) VALUES (1)")
        # Jetzt speichern
        conn.execute("""UPDATE network_settings SET
            printer_ip=?, printer_api_port=?, printer_fluidd_port=?, waage_ip=?
            WHERE id=1""", (printer_ip, api_port, fluidd_port, waage_ip))
        conn.commit()
    log.info(f"Netzwerk gespeichert: {printer_ip} API:{api_port} Fluidd:{fluidd_port} Waage:{waage_ip}")
    return jsonify({"status": "ok"})

# ========== MOONRAKER DRUCKHISTORIE ==========

@app.route("/api/printer/history/sync", methods=["POST"])
def sync_print_history():
    """Druckhistorie von Moonraker holen und lokal speichern"""
    import urllib.request, json as _json
    net = get_network_settings()
    printer_ip = net["printer_ip"]
    api_port   = net.get("printer_api_port", 7125)
    try:
        url = f"http://{printer_ip}:{api_port}/server/history/list?limit=50&order=desc"
        req = urllib.request.urlopen(url, timeout=5)
        data = _json.loads(req.read().decode())
        jobs = data.get("result", {}).get("jobs", [])
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 503

    synced = 0
    with get_db() as conn:
        for job in jobs:
            job_id        = str(job.get("job_id", ""))
            filename      = job.get("filename", "")
            status        = job.get("status", "")
            start_time    = job.get("start_time", 0)
            end_time      = job.get("end_time", 0)
            print_duration = job.get("print_duration", 0)
            filament_mm   = float(job.get("filament_used", 0))
            filament_g    = filament_mm_to_g(filament_mm)  # PLA als Default

            conn.execute("""INSERT OR REPLACE INTO print_history
                (job_id, filename, status, start_time, end_time, print_duration,
                 filament_used_mm, filament_used_g, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (job_id, filename, status, start_time, end_time,
                 print_duration, filament_mm, filament_g))
            synced += 1
        conn.commit()
    log.info(f"Moonraker History: {synced} Jobs synchronisiert")
    return jsonify({"status": "ok", "synced": synced})

@app.route("/api/printer/history")
def get_print_history():
    """Lokale Druckhistorie lesen (mit Spulen-Info)"""
    limit = int(request.args.get("limit", 50))
    with get_db() as conn:
        rows = conn.execute("""
            SELECT ph.*,
                   s.material, s.color, s.brand, s.price_per_kg
            FROM print_history ph
            LEFT JOIN spools s ON ph.spool_uid = s.uid
            ORDER BY ph.start_time DESC LIMIT ?""", (limit,)).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        # Datum formatieren
        import time as _time
        if d.get("start_time"):
            d["start_date"] = datetime.fromtimestamp(d["start_time"]).strftime("%d.%m.%Y %H:%M")
        if d.get("end_time") and d.get("start_time"):
            d["duration_h"] = round((d["end_time"] - d["start_time"]) / 3600, 2)
        # Filamentkosten berechnen wenn Spule bekannt
        if d.get("price_per_kg") and d.get("filament_used_g"):
            d["filament_cost"] = round(d["filament_used_g"] / 1000 * d["price_per_kg"], 3)
        else:
            d["filament_cost"] = None
        result.append(d)
    return jsonify(result)

@app.route("/api/printer/history/<job_id>/spool", methods=["POST"])
def assign_spool_to_job(job_id):
    """Spule einem Druckjob zuordnen und Filamentverbrauch neu berechnen"""
    data = request.get_json(force=True)
    spool_uid = data.get("spool_uid", "")
    notes     = data.get("notes", "")
    with get_db() as conn:
        # Hole Job-Info
        job = conn.execute("SELECT * FROM print_history WHERE job_id=?",
                           (job_id,)).fetchone()
        if not job:
            return jsonify({"status": "error", "message": "Job nicht gefunden"}), 404
        # Hole Spule für Material-Info
        filament_g = job["filament_used_g"]
        if spool_uid:
            spool = conn.execute("SELECT * FROM spools WHERE uid=?",
                                 (spool_uid,)).fetchone()
            if spool:
                # Neu berechnen mit korrektem Material
                filament_g = filament_mm_to_g(
                    job["filament_used_mm"],
                    spool["material"]
                )
        conn.execute("""UPDATE print_history SET
            spool_uid=?, notes=?, filament_used_g=? WHERE job_id=?""",
            (spool_uid, notes, filament_g, job_id))
        conn.commit()
    log.info(f"Job {job_id}: Spule {spool_uid} zugeordnet")
    return jsonify({"status": "ok", "filament_used_g": filament_g})

@app.route("/api/printer/recent_jobs")
def get_recent_jobs():
    """Letzte N abgeschlossene Jobs von Moonraker holen (für Picker)"""
    import urllib.request, json as _json
    limit = int(request.args.get("limit", 5))
    net = get_network_settings()
    printer_ip = net["printer_ip"]
    api_port   = net.get("printer_api_port", 7125)
    try:
        url = f"http://{printer_ip}:{api_port}/server/history/list?limit={limit}&order=desc"
        req = urllib.request.urlopen(url, timeout=4)
        data = _json.loads(req.read().decode())
        jobs = data.get("result", {}).get("jobs", [])
    except Exception as e:
        return jsonify({"status": "error", "message": str(e), "jobs": []}), 503

    result = []
    for job in jobs:
        filament_mm = float(job.get("filament_used", 0))
        filament_g  = filament_mm_to_g(filament_mm)  # PLA als Default-Dichte
        duration_h  = round(float(job.get("print_duration", 0)) / 3600, 3)
        import time as _time
        start_ts = job.get("start_time", 0)
        try:
            start_str = __import__("datetime").datetime.fromtimestamp(start_ts).strftime("%d.%m.%Y %H:%M")
        except Exception:
            start_str = "—"
        result.append({
            "job_id":        job.get("job_id", ""),
            "filename":      job.get("filename", ""),
            "status":        job.get("status", ""),
            "start_date":    start_str,
            "filament_mm":   round(filament_mm, 1),
            "filament_g":    filament_g,
            "duration_h":    duration_h,
            "print_duration": job.get("print_duration", 0),
        })
    return jsonify({"status": "ok", "jobs": result})

@app.route("/api/printer/history/stats")
def print_history_stats():
    """Statistiken aus der Druckhistorie"""
    with get_db() as conn:
        stats = {}
        stats["total_jobs"] = conn.execute(
            "SELECT COUNT(*) as c FROM print_history").fetchone()["c"]
        stats["completed_jobs"] = conn.execute(
            "SELECT COUNT(*) as c FROM print_history WHERE status='completed'").fetchone()["c"]
        tot = conn.execute("""SELECT
            SUM(filament_used_g) as total_g,
            SUM(print_duration) as total_sec,
            SUM(filament_cost) as total_cost
            FROM (
              SELECT ph.filament_used_g,
                     ph.print_duration,
                     CASE WHEN s.price_per_kg IS NOT NULL
                          THEN ph.filament_used_g / 1000.0 * s.price_per_kg
                          ELSE 0 END as filament_cost
              FROM print_history ph
              LEFT JOIN spools s ON ph.spool_uid = s.uid
              WHERE ph.status='completed'
            )""").fetchone()
        stats["total_filament_g"]   = round(tot["total_g"] or 0, 1)
        stats["total_print_hours"]  = round((tot["total_sec"] or 0) / 3600, 1)
        stats["total_filament_cost"] = round(tot["total_cost"] or 0, 2)
    return jsonify(stats)

@app.route("/api/scale/status")
def scale_status():
    """Waage (Pi3) Online-Status – basierend auf letztem Command-Poll Heartbeat"""
    with command_queue["lock"]:
        last_seen = command_queue.get("last_poll")
    if last_seen:
        age = (datetime.now() - datetime.fromisoformat(last_seen)).total_seconds()
        online = age < 10  # Online wenn letzter Poll < 10 Sekunden
        return jsonify({"online": online, "last_seen": last_seen, "age_seconds": round(age, 1)})
    return jsonify({"online": False, "last_seen": None, "age_seconds": None})


# ========== FILAMENT-WAAGE API ==========

@app.route("/api/ping")
def ping():
    return jsonify({"status":"ok","time":datetime.now().isoformat()})

@app.route("/api/spool_detect", methods=["POST"])
def spool_detect():
    data   = request.get_json(force=True)
    uid    = data.get("uid","").upper().strip()
    weight = float(data.get("weight", 0))
    if not uid:
        return jsonify({"status":"error","message":"uid fehlt"}), 400
    log.info(f"spool_detect: {uid} {weight:.1f}g")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM spools WHERE uid=?", (uid,)).fetchone()
        if row is None:
            conn.execute("""INSERT INTO pending_spools (uid, last_weight, seen_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(uid) DO UPDATE SET last_weight=excluded.last_weight,
                seen_at=datetime('now')""", (uid, weight))
            conn.commit()
            return jsonify({"status":"new_spool","uid":uid})
        conn.execute("UPDATE spools SET last_weight=?, updated_at=datetime('now') WHERE uid=?", (weight, uid))
        conn.execute("INSERT INTO weight_log (uid,weight) VALUES (?,?)", (uid, weight))
        conn.execute("DELETE FROM pending_spools WHERE uid=?", (uid,))
        conn.commit()
        result = row_to_dict(row)
        result["status"] = "ok"
        result["last_weight"] = weight
        return jsonify(result)

@app.route("/api/set_spool", methods=["POST"])
def set_spool():
    data    = request.get_json(force=True)
    uid     = data.get("uid","").upper().strip()
    material= data.get("material","PLA").upper().strip()
    color   = data.get("color","UNBEKANNT").upper().strip()
    brand   = data.get("brand","").strip()
    price   = float(data.get("price_per_kg", 20.0))
    empty_w = int(data.get("empty_weight", 220))
    full_w  = int(data.get("full_weight",  1220))
    bed_t   = int(data.get("bed_temp", 60))
    nozzle_t= int(data.get("nozzle_temp", 210))
    last_w  = data.get("last_weight")
    storage_loc = data.get("storage_location", "").strip()
    order_number = data.get("order_number", "").strip()
    brand_color  = data.get("brand_color", "").strip()
    display_color = data.get("display_color", "").strip()
    notes        = data.get("notes", "").strip()
    
    if not uid:
        return jsonify({"status":"error","message":"uid fehlt"}), 400
    if price <= 0:
        return jsonify({"status":"error","message":"Preis/kg muss größer als 0 sein"}), 400
    
    log.info(f"set_spool: {uid} {brand} {material}/{color} {price}€/kg Bed:{bed_t}°C Nozzle:{nozzle_t}°C")
    
    with get_db() as conn:
        # Gewicht aus pending_spools holen wenn nicht vom Formular mitgegeben
        if last_w is None:
            pending = conn.execute(
                "SELECT last_weight FROM pending_spools WHERE uid=?", (uid,)
            ).fetchone()
            if pending and pending["last_weight"] is not None:
                last_w = pending["last_weight"]
                log.info(f"  last_weight aus pending_spools übernommen: {last_w}g")

        # Hersteller in brands-Tabelle speichern falls neu
        if brand:
            conn.execute("INSERT OR IGNORE INTO brands (name) VALUES (?)", (brand,))

        conn.execute("""INSERT INTO spools 
            (uid, material, color, brand, price_per_kg, empty_weight, full_weight, bed_temp, nozzle_temp,
             last_weight, nfc_synced, storage_location, order_number, brand_color, display_color, notes, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,datetime('now'))
            ON CONFLICT(uid) DO UPDATE SET
            material=excluded.material,
            color=excluded.color,
            brand=excluded.brand,
            price_per_kg=excluded.price_per_kg,
            empty_weight=excluded.empty_weight,
            full_weight=excluded.full_weight,
            bed_temp=excluded.bed_temp,
            nozzle_temp=excluded.nozzle_temp,
            last_weight=COALESCE(excluded.last_weight,last_weight),
            nfc_synced=0,
            storage_location=excluded.storage_location,
            order_number=excluded.order_number,
            brand_color=excluded.brand_color,
            display_color=excluded.display_color,
            notes=excluded.notes,
            updated_at=datetime('now')""",
            (uid, material, color, brand, price, empty_w, full_w, bed_t, nozzle_t, last_w,
             storage_loc, order_number, brand_color, display_color, notes))
        
        conn.execute("DELETE FROM pending_spools WHERE uid=?", (uid,))
        conn.commit()
        
        row = conn.execute("SELECT * FROM spools WHERE uid=?", (uid,)).fetchone()
        result = row_to_dict(row)
        result["status"] = "ok"
        return jsonify(result)

@app.route("/api/nfc_sync", methods=["POST"])
def nfc_sync():
    data = request.get_json(force=True)
    uid = data.get("uid","").upper().strip()
    if not uid:
        return jsonify({"status":"error","message":"uid fehlt"}), 400
    log.info(f"nfc_sync: {uid} - NFC-Tag beschrieben")
    with get_db() as conn:
        conn.execute("UPDATE spools SET nfc_synced=1, updated_at=datetime('now') WHERE uid=?", (uid,))
        conn.commit()
    return jsonify({"status":"ok"})

@app.route("/api/spools")
def get_spools():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM spools ORDER BY updated_at DESC").fetchall()
        return jsonify([row_to_dict(r) for r in rows])

@app.route("/api/spools/<uid>")
def get_spool(uid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM spools WHERE uid=?", (uid.upper(),)).fetchone()
        if row:
            return jsonify(row_to_dict(row))
        return jsonify({"status":"error","message":"Spule nicht gefunden"}), 404

@app.route("/api/spool/<uid>", methods=["DELETE"])
def delete_spool(uid):
    with get_db() as conn:
        conn.execute("DELETE FROM spools WHERE uid=?", (uid.upper(),))
        conn.execute("DELETE FROM weight_log WHERE uid=?", (uid.upper(),))
        conn.commit()
    log.info(f"Spule gelöscht: {uid}")
    return jsonify({"status":"ok"})

@app.route("/api/pending")
def get_pending():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM pending_spools ORDER BY seen_at DESC").fetchall()
        return jsonify([dict(r) for r in rows])

@app.route("/api/pending/<uid>", methods=["DELETE"])
def dismiss_pending(uid):
    with get_db() as conn:
        conn.execute("DELETE FROM pending_spools WHERE uid=?", (uid.upper(),))
        conn.commit()
    return jsonify({"status":"ok"})

@app.route("/api/stats")
def get_stats():
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) as cnt FROM spools").fetchone()["cnt"]
        low   = conn.execute("""SELECT COUNT(*) as cnt FROM spools 
            WHERE last_weight IS NOT NULL 
            AND (last_weight - empty_weight) / (full_weight - empty_weight) < 0.2""").fetchone()["cnt"]
        return jsonify({"total_spools": total, "low_spools": low})

@app.route("/api/customers")
def get_customers():
    """NEU v2.13: Alle Kunden aus der Datenbank"""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM customers ORDER BY name").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/customers/upsert", methods=["POST"])
def upsert_customer():
    """NEU v2.13: Kunden anlegen oder Daten aktualisieren"""
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"status": "error", "message": "Name fehlt"}), 400
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM customers WHERE name=?", (name,)).fetchone()
        if existing:
            conn.execute("""UPDATE customers SET street=?, city=?, email=?, phone=?,
                updated_at=datetime('now') WHERE name=?""",
                (data.get("street",""), data.get("city",""),
                 data.get("email",""), data.get("phone",""), name))
            cid = existing["id"]
        else:
            cur = conn.execute("""INSERT INTO customers (name, street, city, email, phone)
                VALUES (?,?,?,?,?)""",
                (name, data.get("street",""), data.get("city",""),
                 data.get("email",""), data.get("phone","")))
            cid = cur.lastrowid
        conn.commit()
    log.info(f"Customer upsert: {name} (id={cid})")
    return jsonify({"status": "ok", "id": cid})

@app.route("/api/brands")
def get_brands():
    with get_db() as conn:
        rows = conn.execute("SELECT name FROM brands ORDER BY name").fetchall()
        return jsonify([r["name"] for r in rows])

@app.route("/api/brands", methods=["POST"])
def add_brand():
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"status": "error", "message": "Name fehlt"}), 400
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO brands (name) VALUES (?)", (name,))
        conn.commit()
    return jsonify({"status": "ok"})


    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) as cnt FROM spools").fetchone()["cnt"]
        low   = conn.execute("""SELECT COUNT(*) as cnt FROM spools 
            WHERE last_weight IS NOT NULL 
            AND (last_weight - empty_weight) / (full_weight - empty_weight) < 0.2""").fetchone()["cnt"]
        return jsonify({"total_spools": total, "low_spools": low})

# ========== AUFTRAGS-API (v2.12) ==========

@app.route("/api/jobs", methods=["GET"])
def get_jobs():
    """Alle Aufträge abrufen"""
    status_filter = request.args.get("status", "")
    with get_db() as conn:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM job_orders WHERE status=? ORDER BY created_at DESC",
                (status_filter,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM job_orders ORDER BY created_at DESC").fetchall()
        jobs = []
        for r in rows:
            job = dict(r)
            # Platten-Info hinzufügen
            plates = conn.execute(
                "SELECT * FROM job_plates WHERE order_id=? ORDER BY plate_number",
                (job["id"],)).fetchall()
            job["plates"] = []
            for p in plates:
                pd = dict(p)
                # Thumbnail als base64 laden
                tp = pd.get("thumbnail_path") or ""
                if tp and Path(tp).exists():
                    try:
                        pd["thumbnail_b64"] = base64.b64encode(Path(tp).read_bytes()).decode('utf-8')
                    except:
                        pd["thumbnail_b64"] = None
                else:
                    pd["thumbnail_b64"] = None
                job["plates"].append(pd)
            job["plates_total"] = len(plates)
            job["plates_done"] = sum(1 for p in plates if p["status"] == "Abgeschlossen")
            job["plates_open"] = sum(1 for p in plates if p["status"] == "Offen")
            jobs.append(job)
    return jsonify(jobs)

@app.route("/api/jobs", methods=["POST"])
def create_job():
    """Neuen Auftrag anlegen"""
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name erforderlich"}), 400
    with get_db() as conn:
        cursor = conn.execute("""INSERT INTO job_orders
            (name, description, status, profit_margin, notes,
             customer_name, customer_street, customer_city, customer_email, customer_phone)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            name,
            data.get("description", ""),
            data.get("status", "Angebot"),
            float(data.get("profit_margin", 30)),
            data.get("notes", ""),
            data.get("customer_name", ""),
            data.get("customer_street", ""),
            data.get("customer_city", ""),
            data.get("customer_email", ""),
            data.get("customer_phone", ""),
        ))
        job_id = cursor.lastrowid
        conn.commit()
    log.info(f"Neuer Auftrag #{job_id}: {name}")
    return jsonify({"id": job_id, "status": "ok"})

@app.route("/api/jobs/<int:job_id>", methods=["GET"])
def get_job(job_id):
    """Einzelnen Auftrag abrufen"""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM job_orders WHERE id=?", (job_id,)).fetchone()
        if not row:
            return jsonify({"error": "Nicht gefunden"}), 404
        job = dict(row)
        plates = conn.execute(
            "SELECT * FROM job_plates WHERE order_id=? ORDER BY plate_number",
            (job_id,)).fetchall()
        job["plates"] = [dict(p) for p in plates]
    return jsonify(job)

@app.route("/api/jobs/<int:job_id>", methods=["PUT"])
def update_job(job_id):
    """Auftrag aktualisieren"""
    data = request.get_json(force=True)
    with get_db() as conn:
        conn.execute("""UPDATE job_orders SET
            name=?, description=?, status=?, profit_margin=?, notes=?,
            customer_name=?, customer_street=?, customer_city=?,
            customer_email=?, customer_phone=?,
            completed_at=CASE WHEN ? IN ('Abgeschlossen','Abgelehnt','Zurückgestellt')
                THEN datetime('now') ELSE completed_at END
            WHERE id=?""", (
            data.get("name"),
            data.get("description", ""),
            data.get("status", "Angebot"),
            float(data.get("profit_margin", 30)),
            data.get("notes", ""),
            data.get("customer_name", ""),
            data.get("customer_street", ""),
            data.get("customer_city", ""),
            data.get("customer_email", ""),
            data.get("customer_phone", ""),
            data.get("status", "Angebot"),
            job_id
        ))
        conn.commit()
    return jsonify({"status": "ok"})

@app.route("/api/jobs/<int:job_id>", methods=["DELETE"])
def delete_job(job_id):
    """Auftrag löschen (inkl. Platten)"""
    with get_db() as conn:
        conn.execute("DELETE FROM job_plates WHERE order_id=?", (job_id,))
        conn.execute("DELETE FROM job_orders WHERE id=?", (job_id,))
        conn.commit()
    return jsonify({"status": "ok"})

# ── Platten-API ───────────────────────────────────────────────────────────────

@app.route("/api/jobs/<int:job_id>/plates", methods=["POST"])
def add_plate(job_id):
    """Neue Druckplatte zum Auftrag hinzufügen"""
    data = request.get_json(force=True)

    # Thumbnail speichern falls vorhanden
    thumb_path = ""
    thumb_b64 = data.get("thumbnail_b64", "")
    if thumb_b64:
        try:
            THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
            import time
            fname = f"plate_{job_id}_{int(time.time()*1000)}.png"
            fpath = THUMBNAIL_DIR / fname
            import base64 as b64mod
            fpath.write_bytes(b64mod.b64decode(thumb_b64))
            thumb_path = str(fpath)
        except Exception as e:
            log.warning(f"Thumbnail speichern fehlgeschlagen: {e}")

    with get_db() as conn:
        result = conn.execute(
            "SELECT COALESCE(MAX(plate_number), 0) + 1 as next FROM job_plates WHERE order_id=?",
            (job_id,)).fetchone()
        next_num = result["next"]
        cursor = conn.execute("""INSERT INTO job_plates
            (order_id, plate_number, status, slicer_weight_g, slicer_time_h,
             pre_post_time_min, profit_margin, filaments, thumbnail_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            job_id, next_num, "Offen",
            float(data.get("slicer_weight_g", 0)),
            float(data.get("slicer_time_h", 0)),
            float(data.get("pre_post_time_min", 0)),
            float(data.get("profit_margin", 30)),
            json.dumps(data.get("filaments", [])),
            thumb_path
        ))
        plate_id = cursor.lastrowid
        conn.commit()
    return jsonify({"id": plate_id, "plate_number": next_num, "status": "ok"})

@app.route("/api/plates/<int:plate_id>", methods=["PUT"])
def update_plate(plate_id):
    """Druckplatte aktualisieren"""
    data = request.get_json(force=True)
    actual_w = float(data.get("actual_weight_g", 0))
    actual_t = float(data.get("actual_time_h", 0))
    status   = data.get("status", "Offen")

    with get_db() as conn:
        conn.execute("""UPDATE job_plates SET
            status=?, slicer_weight_g=?, slicer_time_h=?,
            actual_weight_g=?, actual_time_h=?, pre_post_time_min=?,
            profit_margin=?, moonraker_job_id=?, filaments=?,
            offer_total_cost=?, offer_selling_price=?,
            actual_total_cost=?, actual_selling_price=?,
            failure_notes=?, include_in_costs=?,
            completed_at=CASE WHEN ? = 'Abgeschlossen'
                THEN datetime('now') ELSE completed_at END
            WHERE id=?""", (
            status,
            float(data.get("slicer_weight_g", 0)),
            float(data.get("slicer_time_h", 0)),
            actual_w, actual_t,
            float(data.get("pre_post_time_min", 0)),
            float(data.get("profit_margin", 30)),
            data.get("moonraker_job_id", ""),
            json.dumps(data.get("filaments", [])),
            float(data.get("offer_total_cost", 0)),
            float(data.get("offer_selling_price", 0)),
            float(data.get("actual_total_cost", 0)),
            float(data.get("actual_selling_price", 0)),
            data.get("failure_notes", ""),
            int(data.get("include_in_costs", 1)),
            status, plate_id
        ))
        conn.commit()

    # Korrekturfaktoren neu berechnen wenn Platte abgeschlossen + Istwerte vorhanden
    cf_result = None
    if status == "Abgeschlossen" and actual_w > 0 and actual_t > 0:
        try:
            cf_result = calculate_correction_factors()
        except Exception as e:
            log.warning(f"Korrekturfaktor-Berechnung fehlgeschlagen: {e}")

    resp = {"status": "ok"}
    if cf_result:
        resp["correction_factors"] = cf_result
        resp["correction_updated"] = True
    return jsonify(resp)

@app.route("/api/plates/<int:plate_id>", methods=["DELETE"])
def delete_plate(plate_id):
    """Druckplatte löschen"""
    with get_db() as conn:
        conn.execute("DELETE FROM job_plates WHERE id=?", (plate_id,))
        conn.commit()
    return jsonify({"status": "ok"})

# ── .3mf Upload ───────────────────────────────────────────────────────────────

@app.route("/api/plates/<int:plate_id>/calculate", methods=["POST"])
def calculate_plate(plate_id):
    """
    Berechne Angebotspreis für eine Druckplatte.
    Erwartet: { filaments: [{uid, weight_g}], profit_margin, pre_post_time_min, use_correction }
    Speichert offer_total_cost + offer_selling_price direkt in job_plates.
    """
    data = request.get_json(force=True)
    filaments   = data.get("filaments", [])
    profit_margin = float(data.get("profit_margin", 30))
    pre_post_min  = float(data.get("pre_post_time_min", 0))
    use_correction = data.get("use_correction", False)

    with get_db() as conn:
        plate = conn.execute("SELECT * FROM job_plates WHERE id=?", (plate_id,)).fetchone()
        if not plate:
            return jsonify({"error": "Platte nicht gefunden"}), 404
        plate = dict(plate)

        settings = dict(conn.execute("SELECT * FROM cost_settings WHERE id=1").fetchone())
        cf = dict(conn.execute("SELECT * FROM correction_factors WHERE id=1").fetchone())

        slicer_time = plate["slicer_time_h"]
        slicer_weight = plate["slicer_weight_g"]

        # Korrekturfaktoren
        weight_factor = cf.get("weight_factor", 1.0) if (use_correction and cf.get("samples_count", 0) >= 3) else 1.0
        time_factor   = cf.get("time_factor", 1.0)   if (use_correction and cf.get("samples_count", 0) >= 3) else 1.0
        cost_factor   = cf.get("cost_factor", 1.0)   if (use_correction and cf.get("samples_count", 0) >= 3) else 1.0

        print_hours = slicer_time * time_factor

        # Materialkosten: pro Spule
        material_cost = 0.0
        rolls_detail = []
        total_weight = 0.0

        if filaments:
            for f in filaments:
                uid = str(f.get("uid", "")).upper().strip()
                weight_g = float(f.get("weight_g", 0)) * weight_factor
                if uid and weight_g > 0:
                    spool = conn.execute(
                        "SELECT price_per_kg, material, color FROM spools WHERE uid=?", (uid,)).fetchone()
                    if spool:
                        roll_cost = (weight_g / 1000.0) * spool["price_per_kg"]
                        material_cost += roll_cost
                        total_weight += weight_g
                        rolls_detail.append({
                            "uid": uid, "material": spool["material"],
                            "color": spool["color"],
                            "weight_g": round(weight_g, 1),
                            "cost": round(roll_cost, 2)
                        })
        else:
            # Fallback: Gesamtgewicht × Durchschnitts-Materialpreis
            avg_price = conn.execute(
                "SELECT AVG(price_per_kg) as avg FROM spools").fetchone()["avg"] or 20.0
            total_weight = slicer_weight * weight_factor
            material_cost = (total_weight / 1000.0) * avg_price

        # Drucker-Betriebskosten
        power_cost  = (settings["printer_power_watts"] / 1000.0) * print_hours * settings["power_cost_per_kwh"]
        wear_cost   = (settings["printer_purchase_price"] / max(settings["printer_lifetime_hours"], 1)) * print_hours
        printer_labor = settings.get("printer_labor_cost_per_hour", 0.0) * print_hours
        pre_post_labor = settings.get("pre_post_labor_cost_per_hour", 0.0) * (pre_post_min / 60.0)
        labor_cost = printer_labor + pre_post_labor

        subtotal    = material_cost + power_cost + wear_cost + labor_cost
        failure_cost = subtotal * (settings["failure_rate_percent"] / 100.0)
        total_cost  = (subtotal + failure_cost) * cost_factor

        selling_price = total_cost / (1 - profit_margin / 100.0) if profit_margin < 100 else total_cost

        # Direkt in Platte speichern
        conn.execute("""UPDATE job_plates SET
            filaments=?, offer_total_cost=?, offer_selling_price=?,
            profit_margin=?, pre_post_time_min=?, slicer_weight_g=?, slicer_time_h=?
            WHERE id=?""", (
            json.dumps(filaments),
            round(total_cost, 2),
            round(selling_price, 2),
            profit_margin,
            pre_post_min,
            slicer_weight,
            slicer_time,
            plate_id
        ))
        # Auftrag-Angebotspreis = Summe aller Platten
        order_id = plate["order_id"]
        total_offer = conn.execute(
            "SELECT COALESCE(SUM(offer_selling_price),0) as s FROM job_plates WHERE order_id=?",
            (order_id,)).fetchone()["s"]
        conn.execute("UPDATE job_orders SET offer_price=? WHERE id=?",
            (round(total_offer, 2), order_id))
        conn.commit()

    return jsonify({
        "material_cost": round(material_cost, 2),
        "power_cost": round(power_cost, 2),
        "wear_cost": round(wear_cost, 2),
        "labor_cost": round(labor_cost, 2),
        "failure_cost": round(failure_cost, 2),
        "total_cost": round(total_cost, 2),
        "selling_price": round(selling_price, 2),
        "profit_amount": round(selling_price - total_cost, 2),
        "total_weight_g": round(total_weight, 1),
        "print_hours": round(print_hours, 2),
        "rolls_detail": rolls_detail
    })

@app.route("/api/jobs/parse-3mf", methods=["POST"])
def parse_3mf_upload():
    """
    .3mf hochladen → liefert Plattenstruktur + Filamenttypen + Thumbnails.
    Erkennt automatisch ob ZIP (.3mf) oder Text (G-Code) — Dateiname egal.
    Creality benennt "Exportiere alle geslicten Druckplatten" manchmal als .gcode!
    """
    if "file" not in request.files:
        return jsonify({"error": "Keine Datei"}), 400
    f = request.files["file"]

    import tempfile
    suffix = ".3mf" if f.filename.lower().endswith(".3mf") else ".tmp"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        f.save(tmp.name)
        tmpname = tmp.name

    # Auto-Erkennung: ZIP-Magic-Bytes = PK (0x504B)
    try:
        with open(tmpname, 'rb') as fcheck:
            magic = fcheck.read(2)
        is_zip = (magic == b'PK')
    except:
        is_zip = False

    if is_zip:
        result = parse_3mf(tmpname)
        os.unlink(tmpname)
        if result is None:
            return jsonify({"error": "Datei konnte nicht gelesen werden"}), 400
        return jsonify(result)
    else:
        # Ist eigentlich ein G-Code Text
        result = parse_gcode(tmpname)
        os.unlink(tmpname)
        # In .3mf-ähnliches Format verpacken für das Frontend
        plate = {
            "plate_id": 1, "name": "Platte 1",
            "print_time_h": result["print_time_h"],
            "weight_g": result["total_weight_g"],
            "filaments": result["filaments"],
            "thumbnail_b64": None
        }
        return jsonify({
            "print_time_h":      result["print_time_h"],
            "total_weight_g":    result["total_weight_g"],
            "filaments":         result["filaments"],
            "plates":            [plate],
            "thumbnail_b64":     None,
            "filament_types":    [],
            "filament_densities":[],
            "plate_count":       1,
            "has_slice_data":    result["print_time_h"] > 0 or result["total_weight_g"] > 0
        })


@app.route("/api/jobs/parse-gcode", methods=["POST"])
def parse_gcode_upload():
    """
    G-Code hochladen → liefert Druckzeit + Gewicht.
    Erkennt automatisch ob ZIP (.3mf) oder G-Code Text.
    """
    if "file" not in request.files:
        return jsonify({"error": "Keine Datei"}), 400
    f = request.files["file"]

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".tmp", delete=False) as tmp:
        f.save(tmp.name)
        tmpname = tmp.name

    # Auto-Erkennung
    try:
        with open(tmpname, 'rb') as fcheck:
            magic = fcheck.read(2)
        is_zip = (magic == b'PK')
    except:
        is_zip = False

    if is_zip:
        # Ist eigentlich eine .3mf — an parse_3mf weiterleiten
        result_3mf = parse_3mf(tmpname)
        os.unlink(tmpname)
        if result_3mf is None:
            return jsonify({"error": "Datei konnte nicht gelesen werden"}), 400
        # Gesamtwerte zurückgeben
        return jsonify({
            "print_time_h":   result_3mf["print_time_h"],
            "total_weight_g": result_3mf["total_weight_g"],
            "filaments":      result_3mf["filaments"],
            "plates":         result_3mf["plates"],
            "is_3mf":         True
        })
    else:
        result = parse_gcode(tmpname)
        os.unlink(tmpname)
        return jsonify(result)

# ── Firmendaten ────────────────────────────────────────────────────────────────

@app.route("/api/company", methods=["GET"])
def get_company():
    with get_db() as conn:
        row = conn.execute("SELECT * FROM company_settings WHERE id=1").fetchone()
        return jsonify(dict(row) if row else {})

@app.route("/api/company", methods=["POST"])
def save_company():
    data = request.get_json(force=True)
    with get_db() as conn:
        conn.execute("""UPDATE company_settings SET
            name=?, street=?, city=?, phone=?, email=?, website=?, tax_id=?, bank=?, iban=?
            WHERE id=1""", (
            data.get("name",""), data.get("street",""), data.get("city",""),
            data.get("phone",""), data.get("email",""), data.get("website",""),
            data.get("tax_id",""), data.get("bank",""), data.get("iban","")
        ))
        conn.commit()
    return jsonify({"status": "ok"})


# ── Thumbnails ────────────────────────────────────────────────────────────────

@app.route("/api/jobs/<int:job_id>/pdf")
def export_job_pdf(job_id):
    """Angebot als PDF exportieren"""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor, white, black
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import Table, TableStyle
    from reportlab.lib import colors
    import tempfile, json as _json

    W, H = A4

    with get_db() as conn:
        job = conn.execute("SELECT * FROM job_orders WHERE id=?", (job_id,)).fetchone()
        if not job:
            return jsonify({"error": "Auftrag nicht gefunden"}), 404
        job = dict(job)
        plates = conn.execute(
            "SELECT * FROM job_plates WHERE order_id=? ORDER BY plate_number", (job_id,)
        ).fetchall()
        plates = [dict(p) for p in plates]
        spools = {r["uid"]: dict(r) for r in conn.execute("SELECT * FROM spools").fetchall()}
        company_row = conn.execute("SELECT * FROM company_settings WHERE id=1").fetchone()
        company = dict(company_row) if company_row else {}

    # Modus: Angebot oder Rechnung
    is_invoice = job.get("status") == "Abgeschlossen"
    doc_type   = "RECHNUNG" if is_invoice else "ANGEBOT"

    # Plattendetails mit Filament-Aufschlüsselung
    plate_details = []
    total_selling = 0.0
    for p in plates:
        fils = []
        try: fils = _json.loads(p.get("filaments") or "[]")
        except: pass
        fil_lines = []
        for f in fils:
            uid = str(f.get("uid","")).upper().strip()
            wg  = float(f.get("weight_g", 0))
            sp  = spools.get(uid)
            if sp and wg > 0:
                cost = wg / 1000.0 * sp["price_per_kg"]
                fil_lines.append(f"{sp['material']} {sp['color']} ({wg:.1f}g = {cost:.2f}€)")

        # Rechnung: tatsächliche Werte wenn vorhanden, sonst Slicer-Werte
        if is_invoice and p.get("actual_weight_g") and p["actual_weight_g"] > 0:
            disp_time   = p["actual_time_h"]   or p["slicer_time_h"]
            disp_weight = p["actual_weight_g"]
            disp_price  = p["offer_selling_price"]  # Preis bleibt wie angeboten
        else:
            disp_time   = p["slicer_time_h"]
            disp_weight = p["slicer_weight_g"]
            disp_price  = p["offer_selling_price"]

        plate_details.append({
            "nr":     p["plate_number"],
            "time":   disp_time,
            "weight": disp_weight,
            "price":  disp_price,
            "cost":   p["offer_total_cost"],
            "fils":   fil_lines,
            "status": p["status"],
            "is_actual": is_invoice and p.get("actual_weight_g", 0) > 0
        })
        total_selling += disp_price

    # PDF generieren
    tmpf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    c = rl_canvas.Canvas(tmpf.name, pagesize=A4)
    BLUE    = HexColor("#1e3a8a")
    ACCENT  = HexColor("#3b82f6")
    GREEN   = HexColor("#22c55e")
    LIGHT   = HexColor("#f0f4ff")
    GREY    = HexColor("#6b7280")
    DARKGREY= HexColor("#374151")

    # ── Header-Banner ────────────────────────────────────────────────────────
    c.setFillColor(BLUE)
    c.rect(0, H-42*mm, W, 42*mm, fill=1, stroke=0)

    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(20*mm, H-20*mm, company.get("name") or "FilaStation")
    c.setFont("Helvetica", 9)
    addr_parts = [x for x in [company.get("street",""), company.get("city",""),
                               company.get("phone",""), company.get("email","")] if x]
    c.drawString(20*mm, H-28*mm, "  |  ".join(addr_parts) if addr_parts else "Professionelles Filament-Management")
    if company.get("website"):
        c.drawString(20*mm, H-33*mm, company["website"])

    # Angebot/Rechnung-Badge rechts
    c.setFillColor(ACCENT if not is_invoice else GREEN)
    c.roundRect(W-65*mm, H-36*mm, 52*mm, 20*mm, 3*mm, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(W-39*mm, H-24*mm, doc_type)

    # ── Adressblock ───────────────────────────────────────────────────────────
    y = H - 52*mm

    # Empfänger (Kunde)
    cname  = job.get("customer_name","").strip()
    cstreet= job.get("customer_street","").strip()
    ccity  = job.get("customer_city","").strip()
    cemail = job.get("customer_email","").strip()
    cphone = job.get("customer_phone","").strip()

    c.setFillColor(DARKGREY)
    if cname or cstreet or ccity:
        c.setFont("Helvetica-Bold", 10)
        if cname:   c.drawString(20*mm, y, cname);   y -= 5*mm
        c.setFont("Helvetica", 9)
        if cstreet: c.drawString(20*mm, y, cstreet); y -= 5*mm
        if ccity:   c.drawString(20*mm, y, ccity);   y -= 5*mm
        if cemail:  c.drawString(20*mm, y, cemail);  y -= 5*mm
        if cphone:  c.drawString(20*mm, y, cphone);  y -= 5*mm
    y -= 3*mm

    # Datum + Angebotsnummer rechts
    c.setFont("Helvetica", 9)
    c.setFillColor(GREY)
    from datetime import datetime as _dt
    c.drawRightString(W-20*mm, H-52*mm, f"Datum: {_dt.now().strftime('%d.%m.%Y')}")
    c.drawRightString(W-20*mm, H-57*mm, f"{'Rechnungs' if is_invoice else 'Auftrag'}-Nr.: {job_id:04d}")

    # ── Trennlinie ────────────────────────────────────────────────────────────
    c.setStrokeColor(ACCENT)
    c.setLineWidth(0.5)
    c.line(20*mm, y, W-20*mm, y)
    y -= 6*mm

    # Auftragsbezeichnung
    c.setFillColor(DARKGREY)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(20*mm, y, f"{'Rechnung' if is_invoice else 'Angebot'}: {job['name']}")
    y -= 6*mm
    if job.get("description"):
        c.setFont("Helvetica", 9)
        c.setFillColor(GREY)
        c.drawString(20*mm, y, job["description"])
        y -= 5*mm
    y -= 4*mm

    # ── Platten-Tabelle ───────────────────────────────────────────────────────
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(DARKGREY)
    c.drawString(20*mm, y, "Druckplatten")
    y -= 6*mm

    for pd in plate_details:
        if y < 50*mm:
            c.showPage()
            y = H - 20*mm

        # Platte Header
        c.setFillColor(LIGHT)
        c.rect(20*mm, y-5*mm, W-40*mm, 7*mm, fill=1, stroke=0)
        c.setFillColor(DARKGREY)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(22*mm, y-2*mm,
            f"Platte {pd['nr']}   |   {pd['time']:.2f}h   |   {pd['weight']:.0f}g" +
            ("  ★ Istwerte" if pd.get('is_actual') else ""))
        # Preis rechts
        if pd["price"] > 0:
            c.setFillColor(GREEN)
            c.setFont("Helvetica-Bold", 9)
            c.drawRightString(W-22*mm, y-2*mm, f"{pd['price']:.2f} EUR")
        else:
            c.setFillColor(GREY)
            c.setFont("Helvetica", 9)
            c.drawRightString(W-22*mm, y-2*mm, "nicht kalkuliert")
        y -= 7*mm

        # Filamente
        c.setFillColor(GREY)
        c.setFont("Helvetica", 8)
        if pd["fils"]:
            for fline in pd["fils"]:
                c.drawString(25*mm, y, f"• {fline}")
                y -= 4.5*mm
        else:
            c.drawString(25*mm, y, "• Filament nicht zugewiesen")
            y -= 4.5*mm
        y -= 2*mm

    # ── Gesamtpreis-Box ───────────────────────────────────────────────────────
    y -= 5*mm
    if y < 50*mm:
        c.showPage()
        y = H - 20*mm

    c.setFillColor(GREEN)
    c.roundRect(20*mm, y-14*mm, W-40*mm, 16*mm, 3*mm, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(25*mm, y-8*mm, f"Gesamt{'rechnungs' if is_invoice else 'angebots'}preis (inkl. Gewinn):")
    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(W-25*mm, y-8*mm, f"{total_selling:.2f} EUR")

    # ── Footer ────────────────────────────────────────────────────────────────
    c.setStrokeColor(HexColor("#d1d5db"))
    c.setLineWidth(0.3)
    c.line(20*mm, 22*mm, W-20*mm, 22*mm)
    c.setFillColor(GREY)
    c.setFont("Helvetica", 7)
    footer_left = company.get("name") or "FilaStation"
    if company.get("tax_id"):
        footer_left += f"  |  USt-ID: {company['tax_id']}"
    c.drawString(20*mm, 18*mm, footer_left)
    if company.get("iban"):
        c.drawString(20*mm, 13*mm, f"IBAN: {company['iban']}  {('  |  Bank: ' + company['bank']) if company.get('bank') else ''}")
    c.drawCentredString(W/2, 8*mm, "Erstellt mit FilaStation v2.13 — Andreas Heubach (HEA) — © HEA 2026")

    c.save()
    tmpf.close()

    from flask import send_file
    safe_name = job["name"].replace(" ", "_").replace("/", "-")
    prefix = "Rechnung" if is_invoice else "Angebot"
    return send_file(
        tmpf.name,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{prefix}_{safe_name}.pdf"
    )



def get_thumbnail(job_id):
    """Vorschaubild eines Auftrags als Bild zurückgeben"""
    from flask import send_file
    with get_db() as conn:
        row = conn.execute(
            "SELECT thumbnail_path FROM job_orders WHERE id=?", (job_id,)).fetchone()
    if not row or not row["thumbnail_path"]:
        return jsonify({"error": "Kein Bild"}), 404
    path = Path(row["thumbnail_path"])
    if not path.exists():
        return jsonify({"error": "Datei nicht gefunden"}), 404
    return send_file(path, mimetype="image/png")

# ========== WEBINTERFACE ==========

@app.route("/")
def index():
    return r"""<!DOCTYPE html> <html lang="de"> <head> <meta charset="UTF-8"> <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"> <title>FilaStation v2.13</title> <link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0iVVRGLTgiPz4KPHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI2ODAiIGhlaWdodD0iNjgwIiB2aWV3Qm94PSIwIDAgNjgwIDY4MCI+CgogIDwhLS0gT3V0ZXIgcmluZyAtLT4KICA8Y2lyY2xlIGN4PSIzNDAiIGN5PSIzNDAiIHI9IjI3MCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjE0Ii8+CiAgPGNpcmNsZSBjeD0iMzQwIiBjeT0iMzQwIiByPSIyNTQiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSIyIiBvcGFjaXR5PSIwLjQiLz4KCiAgPCEtLSBJbm5lciBodWIgcmluZ3MgLS0+CiAgPGNpcmNsZSBjeD0iMzQwIiBjeT0iMzQwIiByPSI2NCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjgiLz4KICA8Y2lyY2xlIGN4PSIzNDAiIGN5PSIzNDAiIHI9IjUyIiBmaWxsPSJub25lIiBzdHJva2U9IiNDMDQ4MjgiIHN0cm9rZS13aWR0aD0iMiIgb3BhY2l0eT0iMC40Ii8+CgogIDwhLS0gRmlsYW1lbnQgd2lja2x1bmcgLS0+CiAgPGNpcmNsZSBjeD0iMzQwIiBjeT0iMzQwIiByPSI4MCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjMuNSIgc3Ryb2tlLWRhc2hhcnJheT0iMTggOCIgb3BhY2l0eT0iMC41NSIvPgogIDxjaXJjbGUgY3g9IjM0MCIgY3k9IjM0MCIgcj0iOTIiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSIyLjUiIHN0cm9rZS1kYXNoYXJyYXk9IjIyIDEwIiBvcGFjaXR5PSIwLjM4Ii8+CgogIDwhLS0gTWl0dGVscHVua3QtU3RhbmdlIHZlcnRpa2FsIC0tPgogIDxyZWN0IHg9IjMzMyIgeT0iMjI4IiB3aWR0aD0iMTQiIGhlaWdodD0iMjU2IiByeD0iNyIgZmlsbD0iI0MwNDgyOCIvPgoKICA8IS0tIEhvcml6b250YWxlciBXYWFnYmFsa2VuIC0tPgogIDxyZWN0IHg9IjEzNiIgeT0iMzE2IiB3aWR0aD0iNDA4IiBoZWlnaHQ9IjQ4IiByeD0iMjQiIGZpbGw9IiNDMDQ4MjgiLz4KCiAgPCEtLSBaZWlnZXIgLyBQaXZvdCAtLT4KICA8Y2lyY2xlIGN4PSIzNDAiIGN5PSIzMTYiIHI9IjEyIiBmaWxsPSIjQzA0ODI4Ii8+CiAgPHBvbHlnb24gcG9pbnRzPSIzNDAsMjI4IDMzMCwyNTggMzUwLDI1OCIgZmlsbD0iI0MwNDgyOCIvPgoKICA8IS0tIE1pdHRlbHB1bmt0IC0tPgogIDxjaXJjbGUgY3g9IjM0MCIgY3k9IjM0MCIgcj0iMTgiIGZpbGw9IiNDMDQ4MjgiLz4KICA8Y2lyY2xlIGN4PSIzNDAiIGN5PSIzNDAiIHI9IjgiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI0U4QzhCOCIgc3Ryb2tlLXdpZHRoPSIyLjUiLz4KCiAgPCEtLSBPQkVOIE1JVFRJRzogR2xvYnVzIC0tPgogIDxjaXJjbGUgY3g9IjM0MCIgY3k9IjE3MiIgcj0iNDYiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSI2Ii8+CiAgPGxpbmUgeDE9IjM0MCIgeTE9IjEyNiIgeDI9IjM0MCIgeTI9IjIxOCIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjMuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPGxpbmUgeDE9IjI5NCIgeTE9IjE3MiIgeDI9IjM4NiIgeTI9IjE3MiIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjMuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPGVsbGlwc2UgY3g9IjM0MCIgY3k9IjE1MiIgcng9IjMyIiByeT0iMTAiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSIzIiBzdHJva2UtbGluZWNhcD0icm91bmQiLz4KICA8ZWxsaXBzZSBjeD0iMzQwIiBjeT0iMTkyIiByeD0iMzIiIHJ5PSIxMCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjMiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgogIDxlbGxpcHNlIGN4PSIzNDAiIGN5PSIxNzIiIHJ4PSIxOCIgcnk9IjQ2IiBmaWxsPSJub25lIiBzdHJva2U9IiNDMDQ4MjgiIHN0cm9rZS13aWR0aD0iMyIvPgoKICA8IS0tIExJTktTOiBGaWxhbWVudHNwdWxlIC0tPgogIDxsaW5lIHgxPSIxOTIiIHkxPSIzMzYiIHgyPSIxNjAiIHkyPSI0MjAiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSI1IiBzdHJva2UtbGluZWNhcD0icm91bmQiLz4KICA8bGluZSB4MT0iMTkyIiB5MT0iMzM2IiB4Mj0iMjI0IiB5Mj0iNDIwIiBzdHJva2U9IiNDMDQ4MjgiIHN0cm9rZS13aWR0aD0iNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPHBhdGggZD0iTTE0Niw0MjAgUTE5Miw0NjIgMjM4LDQyMCIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjciIGZpbGw9Im5vbmUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgogIDxsaW5lIHgxPSIxNDYiIHkxPSI0MjAiIHgyPSIyMzgiIHkyPSI0MjAiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSI0IiBzdHJva2UtbGluZWNhcD0icm91bmQiIG9wYWNpdHk9IjAuNSIvPgogIDxjaXJjbGUgY3g9IjE5MiIgY3k9IjQ0OSIgcj0iMjQiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSI2Ii8+CiAgPGNpcmNsZSBjeD0iMTkyIiBjeT0iNDQ5IiByPSIxMCIgZmlsbD0iI0MwNDgyOCIvPgogIDxjaXJjbGUgY3g9IjE5MiIgY3k9IjQ0OSIgcj0iMTciIGZpbGw9Im5vbmUiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSIyIiBzdHJva2UtZGFzaGFycmF5PSI2IDUiIG9wYWNpdHk9IjAuNSIvPgoKICA8IS0tIFJFQ0hUUzogTkZDLVdlbGxlbiAtLT4KICA8bGluZSB4MT0iNDg4IiB5MT0iMzM2IiB4Mj0iNDg4IiB5Mj0iNDAzIiBzdHJva2U9IiNDMDQ4MjgiIHN0cm9rZS13aWR0aD0iNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPGNpcmNsZSBjeD0iNDg4IiBjeT0iNDEyIiByPSI5IiBmaWxsPSIjQzA0ODI4Ii8+CiAgPHBhdGggZD0iTTUwNSwzOTIgUTUyOCw0MTIgNTA1LDQzMiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjYuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPHBhdGggZD0iTTUyMiwzNzYgUTU1OCw0MTIgNTIyLDQ0OCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjUuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPHBhdGggZD0iTTU0MCwzNTggUTU5MCw0MTIgNTQwLDQ2NiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjQuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPGxpbmUgeDE9IjQ2MCIgeTE9IjQyMCIgeDI9IjUxNiIgeTI9IjQyMCIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjMiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgb3BhY2l0eT0iMC4zIi8+Cgo8L3N2Zz4K"> <style> :root { --bg: #0a0a0a; --fg: #e5e5e5; --border: #262626; --accent: #3b82f6; --green: #22c55e; --red: #ef4444; --orange: #f59e0b; --muted: #737373; --hover: #171717; --card: #141414; --purple: #a855f7; } * { margin:0; padding:0; box-sizing:border-box; } body {  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--fg); line-height: 1.5; padding-bottom: 80px; } .container { max-width: 1400px; margin: 0 auto; padding: 1rem; } @media(max-width:640px) { .container { padding: 0.75rem; } }  /* Header */ .header { background: linear-gradient(135deg, #1e3a8a 0%, #3b82f6 100%); padding: 1.5rem 1rem; margin-bottom: 1.5rem; border-radius: 12px; box-shadow: 0 4px 12px rgba(59,130,246,0.2); } .header h1 {  font-size: 1.75rem; font-weight: 700; margin-bottom: 0.5rem; text-shadow: 0 2px 4px rgba(0,0,0,0.3); } @media(max-width:640px) { .header h1 { font-size: 1.25rem; } } .header p { opacity: 0.9; font-size: 0.9rem; } .version-badge { display: inline-block; padding: 0.25rem 0.75rem; background: rgba(255,255,255,0.2); border-radius: 6px; font-size: 0.75rem; font-weight: 600; margin-left: 0.5rem; }  /* Tab Navigation */ .tabs { display: flex; gap: 0.5rem; margin-bottom: 1.5rem; background: var(--card); padding: 0.5rem; border-radius: 12px; overflow-x: auto; -webkit-overflow-scrolling: touch; } .tab { flex: 1; min-width: 120px; padding: 0.75rem 1rem; border: none; background: transparent; color: var(--muted); cursor: pointer; font-size: 0.95rem; font-weight: 500; border-radius: 8px; transition: all 0.2s; white-space: nowrap; } .tab.active { background: var(--accent); color: white; box-shadow: 0 2px 8px rgba(59,130,246,0.3); } .tab:hover:not(.active) { background: var(--hover); }  /* Tab Content */ .tab-content { display: none; } .tab-content.active { display: block; }  /* Cards */ .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 1.5rem; margin-bottom: 1rem; } @media(max-width:640px) { .card { padding: 1rem; } }  /* Stats */ .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; } .stat { background: var(--card); border: 1px solid var(--border); padding: 1.25rem; border-radius: 12px; text-align: center; } .stat-value { font-size: 2rem; font-weight: 700; color: var(--accent); } .stat-label { color: var(--muted); font-size: 0.875rem; margin-top: 0.25rem; } .stat.correction { border-color: var(--purple); } .stat.correction .stat-value { color: var(--purple); }  /* Master Control */ .master-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.75rem; margin-bottom: 1.5rem; } .master-btn { padding: 1rem; border: 1px solid var(--border); border-radius: 8px; background: var(--card); color: var(--fg); cursor: pointer; transition: all 0.2s; font-size: 0.9rem; text-align: center; } .master-btn:hover { background: var(--hover); transform: translateY(-2px); } .master-btn.danger { border-color: var(--red); } .master-btn.danger:hover { background: rgba(239,68,68,0.1); }  /* Form */ .form-grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); } .form-group { display: flex; flex-direction: column; } .form-group label {  font-size: 0.875rem; color: var(--muted); margin-bottom: 0.5rem; font-weight: 500; } .form-group input, .form-group select { padding: 0.75rem; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; color: var(--fg); font-size: 1rem; } .form-group input:focus, .form-group select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(59,130,246,0.1); } .form-group input[type="checkbox"] { width: auto; height: 20px; cursor: pointer; } .checkbox-label { display: flex; align-items: center; gap: 0.5rem; cursor: pointer; }  /* Buttons */ .btn { padding: 0.75rem 1.5rem; border: none; border-radius: 8px; font-size: 0.95rem; font-weight: 500; cursor: pointer; transition: all 0.2s; text-align: center; } .btn-primary { background: var(--accent); color: white; } .btn-primary:hover { background: #2563eb; transform: translateY(-1px); box-shadow: 0 4px 12px rgba(59,130,246,0.3); } .btn-secondary { background: var(--card); color: var(--fg); border: 1px solid var(--border); } .btn-secondary:hover { background: var(--hover); } .btn-danger { background: var(--red); color: white; } .btn-danger:hover { background: #dc2626; } .btn-success { background: var(--green); color: white; } .btn-success:hover { background: #16a34a; }  /* Table */ .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; } table { width: 100%; border-collapse: collapse; font-size: 0.9rem; } th, td { padding: 0.75rem; text-align: left; border-bottom: 1px solid var(--border); } th {  font-weight: 600; color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; } tr:hover { background: var(--hover); }  /* Badge */ .badge { display: inline-block; padding: 0.25rem 0.75rem; border-radius: 6px; font-size: 0.75rem; font-weight: 600; } .badge-PLA { background: #22c55e; color: #000; } .badge-PETG { background: #3b82f6; color: #fff; } .badge-ABS { background: #f59e0b; color: #000; } .badge-ASA { background: #ef4444; color: #fff; } .badge-other { background: var(--muted); color: #fff; } .badge-planned { background: #3b82f6; color: #fff; } .badge-printing { background: #f59e0b; color: #000; } .badge-completed { background: #22c55e; color: #000; }  /* Progress */ .progress-wrap { display: flex; align-items: center; gap: 0.5rem; } .progress-bg { flex: 1; height: 8px; background: var(--border); border-radius: 4px; overflow: hidden; } .progress-fill { height: 100%; transition: width 0.3s; } .pct-text { font-size: 0.75rem; color: var(--muted); min-width: 35px; }  /* Toast */ .toast { position: fixed; bottom: 2rem; right: 2rem; z-index: 1000; padding: 1rem 1.5rem; border-radius: 8px; color: white; box-shadow: 0 4px 12px rgba(0,0,0,0.3); transform: translateY(100px); opacity: 0; transition: all 0.3s; } .toast.show { transform: translateY(0); opacity: 1; } .toast.ok { background: var(--green); } .toast.err { background: var(--red); }  /* Modal */ .overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.8); display: none; align-items: center; justify-content: center; z-index: 999; padding: 1rem; } .overlay.open { display: flex; } .modal { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 2rem; max-width: 600px; width: 100%; max-height: 90vh; overflow-y: auto; } .modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; } .modal-header h2 { font-size: 1.5rem; } .modal-actions { display: flex; gap: 0.75rem; margin-top: 1.5rem; } .modal-actions button { flex: 1; }  /* Cost Calculator Specific */ .cost-result { background: linear-gradient(135deg, #1e3a8a 0%, #3b82f6 100%); padding: 1.5rem; border-radius: 12px; margin-top: 1rem; } .cost-breakdown { display: grid; gap: 0.75rem; margin-top: 1rem; } .cost-item { display: flex; justify-content: space-between; align-items: center; padding: 0.75rem; background: rgba(255,255,255,0.1); border-radius: 8px; } .cost-item-label { font-size: 0.9rem; } .cost-item-value { font-weight: 600; font-size: 1rem; } .cost-total { padding: 1rem; background: rgba(255,255,255,0.2); border-radius: 8px; margin-top: 0.5rem; display: flex; justify-content: space-between; font-size: 1.125rem; font-weight: 700; } .selling-price { background: var(--green); color: white; padding: 1.5rem; border-radius: 12px; text-align: center; margin-top: 1rem; } .selling-price-value { font-size: 2.5rem; font-weight: 700; } .selling-price-label { font-size: 0.9rem; opacity: 0.9; }  /* Delta Display */ .delta-box { background: var(--hover); padding: 1rem; border-radius: 8px; border-left: 4px solid var(--purple); margin-top: 1rem; } .delta-item { display: flex; justify-content: space-between; padding: 0.5rem 0; } .delta-positive { color: var(--green); } .delta-negative { color: var(--red); }  /* Correction Factor Badge */ .cf-badge { display: inline-block; padding: 0.5rem 1rem; background: var(--purple); color: white; border-radius: 8px; font-size: 0.875rem; font-weight: 600; margin-left: 0.5rem; }  /* Responsive */ @media(max-width:640px) { .stats { grid-template-columns: 1fr 1fr; } .master-grid { grid-template-columns: 1fr 1fr; } .form-grid { grid-template-columns: 1fr; } .toast { left: 1rem; right: 1rem; } table { font-size: 0.8rem; } th, td { padding: 0.5rem; } }  .actions { display: flex; gap: 0.5rem; } .btn-icon { padding: 0.5rem 0.65rem; background: rgba(59,130,246,0.15); border: 1px solid rgba(59,130,246,0.4); border-radius: 6px; cursor: pointer; font-size: 1rem; transition: all 0.2s; color: #93c5fd; } .btn-icon:hover { background: rgba(59,130,246,0.3); border-color: #3b82f6; transform: translateY(-1px); } .btn-icon.del { background: rgba(239,68,68,0.15); border: 1px solid rgba(239,68,68,0.4); color: #fca5a5; } .btn-icon.del:hover { background: rgba(239,68,68,0.3); border-color: var(--red); transform: translateY(-1px); }  .uid { font-family: "JetBrains Mono", monospace; } .temp-tag { display: inline-block; padding: 0.25rem 0.5rem; background: var(--hover); border-radius: 4px; font-size: 0.75rem; margin-right: 0.25rem; } .storage-tag { display: inline-block; padding: 0.2rem 0.5rem; background: rgba(59,130,246,0.15); border: 1px solid rgba(59,130,246,0.3); border-radius: 4px; font-size: 0.75rem; color: var(--accent); } .sortable { cursor: pointer; user-select: none; white-space: nowrap; } .sortable:hover { color: var(--fg); } .sort-icon { margin-left: 0.25rem; opacity: 0.5; } .sort-icon.active { opacity: 1; color: var(--accent); } .nfc-indicator { font-size: 1rem; } .nfc-indicator.pending { opacity: 0.5; }  .empty-state { text-align: center; padding: 3rem 1rem; color: var(--muted); } .empty-state p { margin-top: 0.5rem; line-height: 1.6; }  .pending-card { background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); color: white; padding: 1rem; border-radius: 8px; margin-bottom: 0.75rem; display: flex; justify-content: space-between; align-items: center; } .pending-info { flex: 1; } .pending-uid { font-weight: 600; font-size: 1rem; } .pending-weight { font-size: 0.875rem; opacity: 0.9; } .pending-actions { display: flex; gap: 0.5rem; }  .status-bar { display: flex; justify-content: space-between; align-items: center; padding: 0.75rem 1rem; background: var(--card); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 1.5rem; font-size: 0.875rem; } .status-item { display: flex; align-items: center; gap: 0.5rem; } .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); }  .roll-row { display: grid; grid-template-columns: 2fr 1fr 1fr auto; gap: 0.75rem; align-items: end; padding: 0.75rem; background: var(--hover); border-radius: 8px; margin-bottom: 0.5rem; border-left: 3px solid var(--accent); } .roll-row:first-child { border-left-color: var(--green); } .roll-label { font-size: 0.75rem; color: var(--muted); margin-bottom: 0.3rem; font-weight: 500; } .gcode-import { background: var(--hover); border: 2px dashed var(--border); border-radius: 12px; padding: 1.25rem; margin-bottom: 1rem; transition: border-color 0.2s; } .gcode-import:hover { border-color: var(--accent); } .gcode-import.has-data { border-color: var(--green); border-style: solid; background: rgba(34,197,94,0.05); } .gcode-parsed { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 0.75rem; margin-top: 1rem; } .gcode-stat { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 0.75rem; text-align: center; } .gcode-stat-value { font-size: 1.2rem; font-weight: 700; color: var(--accent); } .gcode-stat-label { font-size: 0.75rem; color: var(--muted); margin-top: 0.2rem; } .info-box { background: rgba(168, 85, 247, 0.1); border: 1px solid var(--purple); padding: 1rem; border-radius: 8px; margin-bottom: 1rem; } .info-box-title { font-weight: 600; color: var(--purple); margin-bottom: 0.5rem; display: flex; align-items: center; gap: 0.5rem; } </style> </head> <body> <div class="container"> <div class="header" style="padding:1rem 1.5rem"> <div style="display:flex;align-items:center;gap:1.5rem"> <img src="data:image/svg+xml;base64,PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0iVVRGLTgiPz4KPHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI2ODAiIGhlaWdodD0iNjgwIiB2aWV3Qm94PSIwIDAgNjgwIDY4MCI+CgogIDwhLS0gT3V0ZXIgcmluZyAtLT4KICA8Y2lyY2xlIGN4PSIzNDAiIGN5PSIzNDAiIHI9IjI3MCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjE0Ii8+CiAgPGNpcmNsZSBjeD0iMzQwIiBjeT0iMzQwIiByPSIyNTQiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSIyIiBvcGFjaXR5PSIwLjQiLz4KCiAgPCEtLSBJbm5lciBodWIgcmluZ3MgLS0+CiAgPGNpcmNsZSBjeD0iMzQwIiBjeT0iMzQwIiByPSI2NCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjgiLz4KICA8Y2lyY2xlIGN4PSIzNDAiIGN5PSIzNDAiIHI9IjUyIiBmaWxsPSJub25lIiBzdHJva2U9IiNDMDQ4MjgiIHN0cm9rZS13aWR0aD0iMiIgb3BhY2l0eT0iMC40Ii8+CgogIDwhLS0gRmlsYW1lbnQgd2lja2x1bmcgLS0+CiAgPGNpcmNsZSBjeD0iMzQwIiBjeT0iMzQwIiByPSI4MCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjMuNSIgc3Ryb2tlLWRhc2hhcnJheT0iMTggOCIgb3BhY2l0eT0iMC41NSIvPgogIDxjaXJjbGUgY3g9IjM0MCIgY3k9IjM0MCIgcj0iOTIiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSIyLjUiIHN0cm9rZS1kYXNoYXJyYXk9IjIyIDEwIiBvcGFjaXR5PSIwLjM4Ii8+CgogIDwhLS0gTWl0dGVscHVua3QtU3RhbmdlIHZlcnRpa2FsIC0tPgogIDxyZWN0IHg9IjMzMyIgeT0iMjI4IiB3aWR0aD0iMTQiIGhlaWdodD0iMjU2IiByeD0iNyIgZmlsbD0iI0MwNDgyOCIvPgoKICA8IS0tIEhvcml6b250YWxlciBXYWFnYmFsa2VuIC0tPgogIDxyZWN0IHg9IjEzNiIgeT0iMzE2IiB3aWR0aD0iNDA4IiBoZWlnaHQ9IjQ4IiByeD0iMjQiIGZpbGw9IiNDMDQ4MjgiLz4KCiAgPCEtLSBaZWlnZXIgLyBQaXZvdCAtLT4KICA8Y2lyY2xlIGN4PSIzNDAiIGN5PSIzMTYiIHI9IjEyIiBmaWxsPSIjQzA0ODI4Ii8+CiAgPHBvbHlnb24gcG9pbnRzPSIzNDAsMjI4IDMzMCwyNTggMzUwLDI1OCIgZmlsbD0iI0MwNDgyOCIvPgoKICA8IS0tIE1pdHRlbHB1bmt0IC0tPgogIDxjaXJjbGUgY3g9IjM0MCIgY3k9IjM0MCIgcj0iMTgiIGZpbGw9IiNDMDQ4MjgiLz4KICA8Y2lyY2xlIGN4PSIzNDAiIGN5PSIzNDAiIHI9IjgiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI0U4QzhCOCIgc3Ryb2tlLXdpZHRoPSIyLjUiLz4KCiAgPCEtLSBPQkVOIE1JVFRJRzogR2xvYnVzIC0tPgogIDxjaXJjbGUgY3g9IjM0MCIgY3k9IjE3MiIgcj0iNDYiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSI2Ii8+CiAgPGxpbmUgeDE9IjM0MCIgeTE9IjEyNiIgeDI9IjM0MCIgeTI9IjIxOCIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjMuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPGxpbmUgeDE9IjI5NCIgeTE9IjE3MiIgeDI9IjM4NiIgeTI9IjE3MiIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjMuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPGVsbGlwc2UgY3g9IjM0MCIgY3k9IjE1MiIgcng9IjMyIiByeT0iMTAiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSIzIiBzdHJva2UtbGluZWNhcD0icm91bmQiLz4KICA8ZWxsaXBzZSBjeD0iMzQwIiBjeT0iMTkyIiByeD0iMzIiIHJ5PSIxMCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjMiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgogIDxlbGxpcHNlIGN4PSIzNDAiIGN5PSIxNzIiIHJ4PSIxOCIgcnk9IjQ2IiBmaWxsPSJub25lIiBzdHJva2U9IiNDMDQ4MjgiIHN0cm9rZS13aWR0aD0iMyIvPgoKICA8IS0tIExJTktTOiBGaWxhbWVudHNwdWxlIC0tPgogIDxsaW5lIHgxPSIxOTIiIHkxPSIzMzYiIHgyPSIxNjAiIHkyPSI0MjAiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSI1IiBzdHJva2UtbGluZWNhcD0icm91bmQiLz4KICA8bGluZSB4MT0iMTkyIiB5MT0iMzM2IiB4Mj0iMjI0IiB5Mj0iNDIwIiBzdHJva2U9IiNDMDQ4MjgiIHN0cm9rZS13aWR0aD0iNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPHBhdGggZD0iTTE0Niw0MjAgUTE5Miw0NjIgMjM4LDQyMCIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjciIGZpbGw9Im5vbmUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgogIDxsaW5lIHgxPSIxNDYiIHkxPSI0MjAiIHgyPSIyMzgiIHkyPSI0MjAiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSI0IiBzdHJva2UtbGluZWNhcD0icm91bmQiIG9wYWNpdHk9IjAuNSIvPgogIDxjaXJjbGUgY3g9IjE5MiIgY3k9IjQ0OSIgcj0iMjQiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSI2Ii8+CiAgPGNpcmNsZSBjeD0iMTkyIiBjeT0iNDQ5IiByPSIxMCIgZmlsbD0iI0MwNDgyOCIvPgogIDxjaXJjbGUgY3g9IjE5MiIgY3k9IjQ0OSIgcj0iMTciIGZpbGw9Im5vbmUiIHN0cm9rZT0iI0MwNDgyOCIgc3Ryb2tlLXdpZHRoPSIyIiBzdHJva2UtZGFzaGFycmF5PSI2IDUiIG9wYWNpdHk9IjAuNSIvPgoKICA8IS0tIFJFQ0hUUzogTkZDLVdlbGxlbiAtLT4KICA8bGluZSB4MT0iNDg4IiB5MT0iMzM2IiB4Mj0iNDg4IiB5Mj0iNDAzIiBzdHJva2U9IiNDMDQ4MjgiIHN0cm9rZS13aWR0aD0iNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPGNpcmNsZSBjeD0iNDg4IiBjeT0iNDEyIiByPSI5IiBmaWxsPSIjQzA0ODI4Ii8+CiAgPHBhdGggZD0iTTUwNSwzOTIgUTUyOCw0MTIgNTA1LDQzMiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjYuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPHBhdGggZD0iTTUyMiwzNzYgUTU1OCw0MTIgNTIyLDQ0OCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjUuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPHBhdGggZD0iTTU0MCwzNTggUTU5MCw0MTIgNTQwLDQ2NiIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjQuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPGxpbmUgeDE9IjQ2MCIgeTE9IjQyMCIgeDI9IjUxNiIgeTI9IjQyMCIgc3Ryb2tlPSIjQzA0ODI4IiBzdHJva2Utd2lkdGg9IjMiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgb3BhY2l0eT0iMC4zIi8+Cgo8L3N2Zz4K" style="height:90px;width:90px;flex-shrink:0" alt="Logo"> <div> <h1 style="font-size:1.6rem;font-weight:700;text-shadow:0 2px 4px rgba(0,0,0,0.3);line-height:1.3"> FilaStation v2.13 </h1> <p style="opacity:0.85;font-size:0.875rem;margin-top:0.3rem;text-align:center">Professionelles Filament-Management</p> </div> </div> </div>  <!-- Tab Navigation --> <div class="tabs"> <button class="tab active" onclick="switchTab(&#39;spools&#39;)">⚖️ Spulen</button> <button class="tab" onclick="switchTab(&#39;jobs&#39;)">📋 Aufträge</button>  <button class="tab" onclick="switchTab(&#39;calculations&#39;)">📊 Kalkulationen</button> <button class="tab" onclick="switchTab(&#39;master&#39;)">🎛️ Steuerung</button> <button class="tab" onclick="switchTab(&#39;settings&#39;)">⚙️ Einstellungen</button> </div>  <!-- Status Bar --> <div class="status-bar"> <div class="status-item"> <span class="status-dot" id="srv-dot"></span> <span>Server: <span id="srv-time">—</span></span> </div> <div class="status-item"> <span class="status-dot" id="scale-dot" style="background:var(--muted)"></span> <span style="font-size:0.875rem">Waage: <span id="scale-status">—</span></span> </div> <div class="status-item"> <span class="status-dot" id="printer-dot" style="background:var(--muted)"></span> <a href="#" target="_blank" id="printer-link" style="color:var(--muted);text-decoration:none;font-size:0.875rem">Creality K2+: <span id="printer-status">—</span></a> </div> <div class="status-item"> <span>Spulen: <span id="total-spools">0</span></span> </div> <div class="status-item"> <span>Kalkulationen: <span id="total-calcs">0</span></span> </div> </div>  <!-- TAB 1: SPULEN --> <div id="tab-spools" class="tab-content active"> <!-- Stats --> <div class="stats"> <div class="stat"> <div class="stat-value" id="stat-total">0</div> <div class="stat-label">Spulen gesamt</div> </div> <div class="stat"> <div class="stat-value" id="stat-low" style="color:var(--red)">0</div> <div class="stat-label">Spulen unter 20%</div> </div> </div>  <!-- Pending Spools --> <div id="pending-section"></div>  <!-- Spools Table --> <div class="card"> <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem"> <h3>📦 Spulen-Übersicht</h3> <button class="btn btn-primary" onclick="openModal()">+ Neue Spule</button> </div> <div class="table-wrap"> <table> <thead> <tr> <th class="sortable" onclick="sortSpools(&#39;uid&#39;)">UID <span class="sort-icon" id="sort-uid">↕</span></th> <th class="sortable" onclick="sortSpools(&#39;material&#39;)">Material <span class="sort-icon" id="sort-material">↕</span></th> <th class="sortable" onclick="sortSpools(&#39;color&#39;)">Farbe <span class="sort-icon" id="sort-color">↕</span></th> <th class="sortable" onclick="sortSpools(&#39;price_per_kg&#39;)">Preis/kg <span class="sort-icon" id="sort-price_per_kg">↕</span></th> <th class="sortable" onclick="sortSpools(&#39;last_weight&#39;)">Gewicht <span class="sort-icon" id="sort-last_weight">↕</span></th> <th class="sortable" onclick="sortSpools(&#39;remaining_percent&#39;)">Füllstand <span class="sort-icon" id="sort-remaining_percent">↕</span></th> <th>Temperatur</th> <th>Lagerort</th> <th>NFC</th> <th class="sortable" onclick="sortSpools(&#39;updated_at&#39;)">Aktualisiert <span class="sort-icon" id="sort-updated_at">↕</span></th> <th>Aktionen</th> </tr> </thead> <tbody id="tbody"></tbody> </table> </div> </div> </div>   <div id="tab-jobs" class="tab-content"> <div class="stats"> <div class="stat"> <div class="stat-value" id="jobs-stat-total">0</div> <div class="stat-label">Aufträge gesamt</div> </div> <div class="stat"> <div class="stat-value" id="jobs-stat-open" style="color:var(--orange)">0</div> <div class="stat-label">In Warteschlange</div> </div> <div class="stat"> <div class="stat-value" id="jobs-stat-printing" style="color:var(--accent)">0</div> <div class="stat-label">In Druck</div> </div> <div class="stat"> <div class="stat-value" id="jobs-stat-done" style="color:var(--green)">0</div> <div class="stat-label">Abgeschlossen</div> </div> </div> <div class="card"> <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem"> <h3>📋 Auftrags-Warteschlange</h3> <button class="btn btn-primary" onclick="openNewJobModal()">+ Neuer Auftrag</button> </div> <div id="jobs-list"><div class="empty-state"><p>Noch keine Aufträge vorhanden.</p></div></div> </div> <div class="card" style="margin-top:1rem"> <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem"> <h3 style="color:var(--muted)">🗄️ Archiv</h3> <button class="btn btn-secondary" style="font-size:0.8rem;padding:0.4rem 0.8rem" onclick="toggleArchive()">Archiv anzeigen</button> </div> <div id="jobs-archive" style="display:none"></div> </div> </div>  <!-- TAB 3: KALKULATIONEN --> <div id="tab-calculations" class="tab-content">  <!-- Statistics --> <div class="stats"> <div class="stat"> <div class="stat-value" id="stats-total-calcs">0</div> <div class="stat-label">Kalkulationen</div> </div> <div class="stat"> <div class="stat-value" id="stats-completed">0</div> <div class="stat-label">Abgeschlossen</div> </div> <div class="stat correction"> <div class="stat-value" id="stats-weight-dev">±0%</div> <div class="stat-label">Ø Gewichts-Abweichung</div> </div> <div class="stat correction"> <div class="stat-value" id="stats-time-dev">±0%</div> <div class="stat-label">Ø Zeit-Abweichung</div> </div> </div>  <div class="card"> <h3 style="margin-bottom:1rem">📊 Gespeicherte Kalkulationen</h3> <div class="table-wrap"> <table> <thead> <tr> <th>Name</th> <th>Material</th> <th>Slicer (g/h)</th> <th>Tatsächlich (g/h)</th> <th>Kosten</th> <th>VK-Preis</th> <th>Status</th> <th>Erstellt</th> <th>Aktionen</th> </tr> </thead> <tbody id="calculations-tbody"></tbody> </table> </div> </div> </div>  <!-- TAB 4: MASTER-STEUERUNG --> <div id="tab-master" class="tab-content"> <div class="card"> <h3 style="margin-bottom:1rem">🎛️ Master-Steuerung</h3> <p style="color:var(--muted);font-size:0.875rem;margin-bottom:1.25rem"> Steuerbefehle werden direkt an die Waage (Pi3) gesendet und beim nächsten Polling abgeholt. </p> <div class="master-grid"> <button class="master-btn" onclick="sendMasterCommand(&#39;tare&#39;)"> <div style="font-size:1.5rem">⚖️</div> <div>TARE</div> <div style="font-size:0.75rem;color:var(--muted)">Waage nullen</div> </button> <button class="master-btn" onclick="sendMasterCommand(&#39;calibrate&#39;)"> <div style="font-size:1.5rem">🔧</div> <div>KALIBRIERUNG</div> <div style="font-size:0.75rem;color:var(--muted)">500g Routine</div> </button> <button class="master-btn danger" onclick="confirmMasterCommand(&#39;reboot&#39;)"> <div style="font-size:1.5rem">🔄</div> <div>NEUSTART</div> <div style="font-size:0.75rem;color:var(--muted)">Pi3 Reboot</div> </button> <button class="master-btn danger" onclick="confirmMasterCommand(&#39;shutdown&#39;)"> <div style="font-size:1.5rem">⏻</div> <div>SHUTDOWN</div> <div style="font-size:0.75rem;color:var(--muted)">Pi3 aus</div> </button> </div> </div> </div>  <!-- TAB 6: DRUCKHISTORIE --> <div id="tab-history" class="tab-content"> <div class="stats"> <div class="stat"> <div class="stat-value" id="hist-total">0</div> <div class="stat-label">Drucke gesamt</div> </div> <div class="stat"> <div class="stat-value" id="hist-filament">0g</div> <div class="stat-label">Filament gesamt</div> </div> <div class="stat"> <div class="stat-value" id="hist-hours">0h</div> <div class="stat-label">Druckzeit gesamt</div> </div> <div class="stat"> <div class="stat-value" id="hist-cost">0.00&#8364;</div> <div class="stat-label">Materialkosten</div> </div> </div> <div class="card"> <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem"> <h3>&#128247; Druckhistorie</h3> <div style="display:flex;gap:0.5rem"> <button class="btn btn-secondary" onclick="syncMoonrakerHistory()" style="padding:0.5rem 1rem;font-size:0.85rem">&#128257; Moonraker sync</button> <button class="btn btn-secondary" onclick="loadHistory()" style="padding:0.5rem 1rem;font-size:0.85rem">&#128260; Aktualisieren</button> </div> </div> <p style="color:var(--muted);font-size:0.825rem;margin-bottom:1rem">Eintr&#228;ge werden automatisch aus &#34;Speichern &amp; Lernen&#34; erzeugt. Zus&#228;tzlich kann die Moonraker-Druckhistorie synchronisiert werden.</p> <div class="table-wrap"> <table> <thead> <tr> <th>Datum</th> <th>Dateiname / Name</th> <th>Status</th> <th>Druckzeit</th> <th>Filament</th> <th>Materialkosten</th> <th>Spule</th> <th>Quelle</th> </tr> </thead> <tbody id="history-tbody"></tbody> </table> </div> </div> </div>  <!-- TAB 5: EINSTELLUNGEN --> <div id="tab-settings" class="tab-content"> <div class="card"> <h3 style="margin-bottom:1.5rem">⚙️ Kostenrechner-Einstellungen</h3>  <div class="form-grid"> <div class="form-group"> <label>Strompreis (€/kWh)</label> <input type="number" id="set-power-cost" step="0.01" min="0"> </div>  <div class="form-group"> <label>Drucker-Leistung (Watt)</label> <input type="number" id="set-power-watts" step="1" min="0"> </div>  <div class="form-group"> <label>Drucker-Kaufpreis (€)</label> <input type="number" id="set-printer-price" step="1" min="0"> </div>  <div class="form-group"> <label>Lebenszeit Drucker (Std.)</label> <input type="number" id="set-lifetime" step="100" min="0"> </div>  <div class="form-group"> <label>Fehldruckrate (%)</label> <input type="number" id="set-failure" step="1" min="0" max="100"> </div>  <div class="form-group"> <label>Standard-Gewinnmarge (%)</label> <input type="number" id="set-margin" step="1" min="0" max="100"> </div>  </div>  <!-- Stundensätze in eigener Zeile --> <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:1rem;margin-top:1rem;padding-top:1rem;border-top:1px solid var(--border)"> <div class="form-group"> <label>🖨️ Druckerstunden-Satz (€/h)</label> <input type="number" id="set-printer-labor" step="0.1" min="0" title="Kosten pro Druckstunde (Überwachung, Energie-Overhead etc.)"> </div> <div class="form-group"> <label>🔧 Vor-/Nachbearbeitung Satz (€/h)</label> <input type="number" id="set-pre-post-labor" step="0.1" min="0" title="Stundensatz für Vor- und Nachbearbeitungszeit"> </div> </div>  <button class="btn btn-primary" onclick="saveSettings()" style="width:100%;margin-top:1rem"> Einstellungen speichern </button> </div>  <div class="card"> <h3 style="margin-bottom:1rem">💵 Material-Standardpreise</h3> <p style="color:var(--muted);font-size:0.875rem;margin-bottom:1rem"> Hinweis: Beim Anlegen von Spulen wird der Preis/kg direkt in der Spule gespeichert. Diese Standardpreise dienen nur als Fallback. </p> <div id="material-prices"></div> </div> <div class="card" style="margin-top:1rem"> <h3 style="margin-bottom:1rem">&#127760; Netzwerk-Konfiguration</h3> <p style="color:var(--muted);font-size:0.875rem;margin-bottom:1rem">Drucker-API (Moonraker Port 7125) und Fluidd-Port (4408) werden getrennt gespeichert.</p> <div class="form-grid"> <div class="form-group"> <label>Drucker-IP (K2+)</label> <input type="text" id="net-printer-ip" placeholder="192.168.178.57"> </div> <div class="form-group"> <label>Moonraker API-Port</label> <input type="number" id="net-api-port" value="7125"> </div> <div class="form-group"> <label>Fluidd Web-Port</label> <input type="number" id="net-fluidd-port" value="4408"> </div> <div class="form-group"> <label>Waage-IP (Pi 3)</label> <input type="text" id="net-waage-ip" placeholder="192.168.178.65"> </div> </div> <button class="btn btn-primary" onclick="saveNetworkSettings()" style="width:100%;margin-top:1rem">Netzwerk speichern</button> </div> <div class="card" style="margin-top:1rem"> <h3 style="margin-bottom:1rem">&#127968; Firmendaten (fuer PDF-Angebot)</h3> <div class="form-grid"> <div class="form-group" style="grid-column:1/-1"> <label>Firmenname</label> <input type="text" id="co-name" placeholder="z.B. HEA 3D-Druck"> </div> <div class="form-group"> <label>Strasse + Hausnummer</label> <input type="text" id="co-street" placeholder="Musterstrasse 1"> </div> <div class="form-group"> <label>PLZ + Ort</label> <input type="text" id="co-city" placeholder="70374 Stuttgart"> </div> <div class="form-group"> <label>Telefon</label> <input type="text" id="co-phone" placeholder="+49 711 ..."> </div> <div class="form-group"> <label>E-Mail</label> <input type="text" id="co-email" placeholder="info@beispiel.de"> </div> <div class="form-group"> <label>Website</label> <input type="text" id="co-website" placeholder="www.beispiel.de"> </div> <div class="form-group"> <label>USt-ID</label> <input type="text" id="co-taxid" placeholder="DE123456789"> </div> <div class="form-group"> <label>Bank</label> <input type="text" id="co-bank" placeholder="Sparkasse Stuttgart"> </div> <div class="form-group" style="grid-column:1/-1"> <label>IBAN</label> <input type="text" id="co-iban" placeholder="DE12 3456 7890 1234 5678 90"> </div> </div> <button class="btn btn-primary" onclick="saveCompanySettings()" style="width:100%;margin-top:1rem">Firmendaten speichern</button> </div>  </div> </div>  <!-- Spule Modal --> <div class="overlay" id="overlay"> <div class="modal"> <div class="modal-header"> <h2 id="modal-title">Neue Spule <span id="modal-uid"></span></h2> </div> <div class="form-grid"> <div class="form-group"> <label>UID *</label> <input type="text" id="f-uid"> </div> <div class="form-group"> <label>Material *</label> <select id="f-material" onchange="suggestPrice()"> <option>PLA</option> <option>PLA+</option> <option>PETG</option> <option>PETG-CF</option> <option>ABS</option> <option>ABS-CF</option> <option>ASA</option> <option>TPU</option> <option>NYLON</option> <option>PC</option> <option>HIPS</option> <option>PVA</option> </select> </div> <div class="form-group"> <label>Farbe * (Herstellerbezeichnung)</label> <input type="text" id="f-color" placeholder="z.B. Metallic Bronze"> </div> <div class="form-group"> <label>&#127775; Anzeigefarbe (Drucker/Slicer)</label> <select id="f-display-color"> <option value="">-- nicht festgelegt --</option> <option value="White">White / Wei&#223;</option> <option value="Black">Black / Schwarz</option> <option value="Grey">Grey / Grau</option> <option value="Red">Red / Rot</option> <option value="Orange">Orange</option> <option value="Yellow">Yellow / Gelb</option> <option value="Green">Green / Gr&#252;n</option> <option value="Blue">Blue / Blau</option> <option value="Purple">Purple / Lila</option> <option value="Pink">Pink</option> <option value="Brown">Brown / Braun</option> <option value="Gold">Gold</option> <option value="Silver">Silver / Silber</option> <option value="Beige">Beige</option> <option value="Transparent">Transparent</option> </select> </div> <div class="form-group"> <label>Hersteller</label> <input type="text" id="f-brand" list="brand-list" autocomplete="off"> <datalist id="brand-list"></datalist> </div> <div class="form-group"> <label>⭐ Preis/kg (€) * WICHTIG!</label> <input type="number" id="f-price" step="0.1" min="0.1" required> </div> <div class="form-group"> <label>Bett-Temp (°C)</label> <input type="number" id="f-bed"> </div> <div class="form-group"> <label>Düsen-Temp (°C)</label> <input type="number" id="f-nozzle"> </div> <div class="form-group"> <label>Leergewicht (g)</label> <input type="number" id="f-empty"> </div> <div class="form-group"> <label>Vollgewicht (g)</label> <input type="number" id="f-full"> </div> <div class="form-group"> <label>Aktuelles Gewicht (g)</label> <input type="number" id="f-last"> </div> <div class="form-group"> <label>📍 Lagerort</label> <select id="f-storage"> <option value="">-- kein Lagerort --</option> <option value="Regal 1">Regal 1</option> <option value="Regal 2">Regal 2</option> <option value="Regal 3">Regal 3</option> <option value="Schublade">Schublade</option> <option value="Drucker">Am Drucker</option> <option value="Sonstige">Sonstige</option> </select> </div> <div class="form-group"> <label>&#128717; Bestellnummer (Hersteller)</label> <input type="text" id="f-order-number" placeholder="z.B. B09XYZ123"> </div> <div class="form-group" style="grid-column:1/-1"> <label>&#128221; Bemerkungen</label> <textarea id="f-notes" rows="2" style="padding:0.75rem;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--fg);font-size:1rem;resize:vertical;" placeholder="Optionale Notizen..."></textarea> </div> </div> <div style="color:var(--muted);font-size:0.875rem;margin-top:1rem"> * = Pflichtfelder </div> <div class="modal-actions"> <button class="btn btn-secondary" onclick="closeModal()">Abbrechen</button> <button class="btn btn-primary" onclick="saveSpool()">Speichern</button> </div> </div> </div>  <!-- Save Calculation Modal --> <div class="overlay" id="save-calc-overlay"> <div class="modal"> <div class="modal-header"> <h2>Kalkulation speichern</h2> </div> <div class="form-grid"> <div class="form-group"> <label>Name *</label> <input type="text" id="save-calc-name" placeholder="z.B. Benchy, Vase, etc."> </div> <div class="form-group" style="grid-column: 1/-1"> <label>Beschreibung (optional)</label> <input type="text" id="save-calc-desc" placeholder="Zusätzliche Details"> </div> <div class="form-group"> <label>Status</label> <select id="save-calc-status"> <option value="planned">Geplant</option> <option value="printing">Wird gedruckt</option> <option value="completed">Abgeschlossen</option> </select> </div> <div class="form-group"> <label>Druckdatum (optional)</label> <input type="date" id="save-calc-date"> </div> </div> <div class="modal-actions"> <button class="btn btn-secondary" onclick="closeSaveCalcModal()">Abbrechen</button> <button class="btn btn-primary" onclick="saveCalculation()">Speichern</button> </div> </div> </div>  <!-- Update Calculation Modal --> <div class="overlay" id="update-calc-overlay"> <div class="modal"> <div class="modal-header"> <h2>Tatsächliche Werte nachtragen</h2> </div> <p style="color:var(--muted);margin-bottom:1rem"> Trage die tatsächlichen Werte ein nachdem der Druck abgeschlossen ist. Das System lernt automatisch aus den Abweichungen! </p> <div class="form-grid"> <div class="form-group"> <label>Tatsächliches Gewicht (g)</label> <input type="number" id="update-actual-weight" step="0.1" min="0"> </div> <div class="form-group"> <label>Tatsächliche Druckzeit (Stunden)</label> <input type="number" id="update-actual-time" step="0.1" min="0"> </div> <div class="form-group"> <label>Status</label> <select id="update-calc-status"> <option value="planned">Geplant</option> <option value="printing">Wird gedruckt</option> <option value="completed">Abgeschlossen</option> </select> </div> </div> <div class="modal-actions"> <button class="btn btn-secondary" onclick="closeUpdateCalcModal()">Abbrechen</button> <button class="btn btn-success" onclick="updateCalculation()">Speichern & Lernen</button> </div> </div> </div> <!-- Neuer Auftrag Modal --> <div class="overlay" id="new-job-overlay"> <div class="modal" style="max-width:680px"> <div class="modal-header"> <h2>&#128203; Neuer Auftrag</h2> <button class="btn-icon" onclick="closeNewJobModal()" style="font-size:1.2rem">&#10005;</button> </div> <div class="form-grid"> <div class="form-group" style="grid-column:1/-1"> <label>Auftragsname *</label> <input type="text" id="new-job-name" placeholder="z.B. Vasen-Set fuer Kunde Mayer"> </div> <div class="form-group" style="grid-column:1/-1"> <label>Beschreibung</label> <input type="text" id="new-job-desc" placeholder="Optionale Details zum Auftrag"> </div> </div>  <div style="margin-top:1rem;padding-top:1rem;border-top:1px solid var(--border)"> <div style="font-weight:600;margin-bottom:0.5rem">&#128193; .3mf Projektdatei importieren</div> <div style="font-size:0.85rem;color:var(--muted);margin-bottom:0.75rem">Creality Print: Datei &rarr; Exportieren &rarr; <strong>Exportieren aller geslicten Druckplatten...</strong></div> <label style="cursor:pointer"> <input type="file" id="new-job-3mf-input" accept="*" style="display:none" onchange="parse3mfFile(this)"> <span class="btn btn-secondary" style="padding:0.5rem 1rem;font-size:0.875rem">&#128193; .3mf Datei waehlen (geslict)</span> </label> </div> <div id="new-job-3mf-result"></div> <input type="hidden" id="new-job-time" value="0"> <input type="hidden" id="new-job-weight" value="0"> <input type="hidden" id="new-job-thumbnail" value=""> <div class="modal-actions"> <button class="btn btn-secondary" onclick="closeNewJobModal()">Abbrechen</button> <button class="btn btn-primary" onclick="saveNewJob()">Auftrag anlegen</button> </div> </div> </div>  <!-- Auftrag Bearbeiten Modal -->
<div class="overlay" id="edit-job-overlay">
  <div class="modal" style="max-width:680px">
    <div class="modal-header">
      <h2>&#9998; Auftrag bearbeiten</h2>
      <button class="btn-icon" onclick="closeEditJobModal()" style="font-size:1.2rem">&#10005;</button>
    </div>
    <input type="hidden" id="edit-job-id">
    <div class="form-grid">
      <div class="form-group" style="grid-column:1/-1">
        <label>Auftragsname *</label>
        <input type="text" id="edit-job-name">
      </div>
      <div class="form-group" style="grid-column:1/-1">
        <label>Beschreibung</label>
        <input type="text" id="edit-job-desc">
      </div>
    </div>
    <div style="margin-top:0.75rem;padding-top:0.75rem;border-top:1px solid var(--border)">
      <div style="font-weight:600;margin-bottom:0.5rem;font-size:0.9rem">&#128100; Kundendaten</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.75rem">
        <div class="form-group" style="grid-column:1/-1;margin:0">
          <label>Kundenname</label>
          <input type="text" id="edit-job-customer-name" placeholder="z.B. Max Mustermann" list="customer-name-list" autocomplete="off" oninput="onCustomerNameInput()"> <datalist id="customer-name-list"></datalist>
        </div>
        <div class="form-group" style="margin:0">
          <label>Strasse</label>
          <input type="text" id="edit-job-customer-street" placeholder="Musterstrasse 1">
        </div>
        <div class="form-group" style="margin:0">
          <label>PLZ + Ort</label>
          <input type="text" id="edit-job-customer-city" placeholder="70374 Stuttgart">
        </div>
        <div class="form-group" style="margin:0">
          <label>E-Mail</label>
          <input type="text" id="edit-job-customer-email" placeholder="kunde@beispiel.de">
        </div>
        <div class="form-group" style="margin:0">
          <label>Telefon</label>
          <input type="text" id="edit-job-customer-phone" placeholder="+49 ...">
        </div>
      </div>
    </div>
    <div class="modal-actions">
      <button class="btn btn-secondary" onclick="closeEditJobModal()">Abbrechen</button>
      <button class="btn btn-primary" onclick="saveEditJob()">Speichern</button>
    </div>
  </div>
</div>

<!-- Platten-Detail Modal --> <div class="overlay" id="plate-overlay"> <div class="modal" style="max-width:720px"> <div class="modal-header"> <h2 id="plate-modal-title">Platte</h2> <button class="btn-icon" onclick="closePlateModal()" style="font-size:1.2rem">&#10005;</button> </div> <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1rem"> <div class="form-group"> <label>Gewicht Slicer (g)</label> <input type="number" id="plate-slicer-weight" step="0.1" min="0"> </div> <div class="form-group"> <label>Druckzeit Slicer (h)</label> <input type="number" id="plate-slicer-time" step="0.1" min="0"> </div> <div class="form-group"> <label>Gewinnmarge (%)</label> <input type="number" id="plate-margin" step="1" min="0" max="100"> </div> <div class="form-group"> <label>Vor-/Nacharbeit (Min.)</label> <input type="number" id="plate-pre-post" step="1" min="0"> </div> <div class="form-group"> <label>Status</label> <select id="plate-status" onchange="toggleFailureSection(this.value)"> <option>Offen</option> <option>In Druck</option> <option>Abgeschlossen</option> <option>Fehlschlag</option> </select> </div> <div class="form-group" style="grid-column:1/-1"> <div id="plate-cf-info" style="display:none;padding:0.6rem 0.75rem;border-radius:6px;font-size:0.8rem;margin-bottom:0.5rem"></div> <label>Korrekturfaktor anwenden</label> <div style="display:flex;align-items:center;gap:0.5rem;height:42px"> <input type="checkbox" id="plate-use-correction" style="width:20px;height:20px"> <span id="plate-cf-label" style="font-size:0.8rem;color:var(--muted)"></span> </div> </div> </div> <div style="margin-bottom:1rem"> <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.5rem"> <label style="font-weight:600">Filamente / Spulenzuweisung</label> <div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap"><button class="btn btn-secondary" onclick="addPlateFilament()" style="padding:0.3rem 0.75rem;font-size:0.8rem">+ Filament</button><label style="cursor:pointer"><input type="file" id="plate-gcode-input" accept=".gcode,.gc,.g,.gco" style="display:none" onchange="loadGcodeIntoPlate(this)"><span class="btn btn-secondary" style="padding:0.3rem 0.75rem;font-size:0.8rem">&#128196; G-Code laden</span></label><span id="plate-gcode-name" style="display:none;font-size:0.75rem;color:var(--green);font-weight:600"></span><button id="plate-gcode-clear" onclick="clearPlateGcode()" style="display:none;padding:0.15rem 0.5rem;font-size:0.7rem;background:rgba(239,68,68,0.2);border:1px solid var(--red);border-radius:4px;color:var(--red);cursor:pointer" title="G-Code zurücksetzen">&#10005;</button></div> </div> <div id="plate-filaments"></div> </div> <button class="btn btn-primary" id="plate-calc-btn" onclick="calculatePlate()" style="width:100%">Kosten berechnen &amp; speichern</button> <div id="plate-result" style="display:none"></div> <div id="plate-failure-section" style="display:none;margin-top:1rem;padding:1rem;background:rgba(239,68,68,0.1);border:1px solid var(--red);border-radius:8px"> <div style="font-weight:600;color:var(--red);margin-bottom:0.75rem">&#128680; Fehlschlag-Dokumentation (intern)</div> <div class="form-group"> <label>Notiz zum Fehlschlag (nicht auf Kundenrechnung)</label> <textarea id="plate-failure-notes" rows="3" style="padding:0.75rem;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--fg);font-size:0.9rem;width:100%;resize:vertical" placeholder="Was ist schiefgelaufen? Material, Temperatur, Haftung..."></textarea> </div> <div class="form-group" style="margin-top:0.5rem"> <label><input type="checkbox" id="plate-include-costs" style="margin-right:0.5rem">Kosten trotzdem berechnen (Materialkosten abschreiben)</label> </div> </div> <div style="margin-top:1rem;padding-top:1rem;border-top:1px solid var(--border)"> <div style="font-weight:600;margin-bottom:0.75rem">&#10004; Tatsaechliche Werte (nach Druck)</div> <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem"> <div class="form-group"> <label>Tatsaechl. Gewicht (g)</label> <input type="number" id="plate-actual-weight" step="0.1" min="0" placeholder="nach Druck"> </div> <div class="form-group"> <label>Tatsaechl. Druckzeit (h)</label> <input type="number" id="plate-actual-time" step="0.1" min="0" placeholder="nach Druck"> </div> </div> </div> <div class="modal-actions"> <button class="btn btn-secondary" onclick="closePlateModal()">Schliessen</button> <button class="btn btn-success" onclick="savePlateStatus()">Status speichern</button> </div> </div> </div>   <div class="toast" id="toast"></div>  <script>
let editUid = null;
let currentCalculation = null;
let currentUpdateCalcId = null;
let materialPrices = {};
let spoolsData = [];
let sortColumn = 'updated_at';
let sortAsc = false;

// ========== INITIALIZATION ==========

async function init() {
  try { await loadMaterialPrices(); } catch(e) { console.warn('loadMaterialPrices:', e); }
  try { await loadBrands(); } catch(e) { console.warn('loadBrands:', e); }
  try { load(); } catch(e) { console.warn('load:', e); }
  updateStatus();
  updatePrinterStatus();
  updateScaleStatus();
  setInterval(load, 15000);
  setInterval(updateStatus, 10000);
  setInterval(updatePrinterStatus, 15000);
  setInterval(updateScaleStatus, 15000);
}

async function loadBrands() {
  const r = await fetch('/api/brands');
  const brands = await r.json();
  const dl = document.getElementById('brand-list');
  if (dl) {
    dl.innerHTML = brands.map(b => `<option value="${b}">`).join('');
  }
  // Trick: show all options on focus by briefly clearing value
  const input = document.getElementById('f-brand');
  if (input) {
    input.addEventListener('focus', function() {
      const val = this.value;
      this.value = '';
      setTimeout(() => {
        if (this.value === '') this.value = val;
      }, 200);
    });
    input.addEventListener('input', function() {
      // Auto-restore if user clears field
    });
  }
}

async function loadMaterialPrices() {
  const r = await fetch('/api/cost/material-prices');
  const prices = await r.json();
  materialPrices = {};
  prices.forEach(p => {
    materialPrices[p.material] = {
      price: p.price_per_kg,
      bed_temp: p.bed_temp || 60,
      nozzle_temp: p.nozzle_temp || 210
    };
  });
}

// ========== TAB SWITCHING ==========

function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById(`tab-${tab}`).classList.add('active');
  
  if (tab === 'calculations') {
    loadCalculations();
    loadStatistics();
  }
  if (tab === 'settings') loadSettings();
  if (tab === 'history') loadHistory();
  if (tab === 'jobs') loadJobs();
}

// ========== FILAMENT-WAAGE FUNCTIONS ==========

async function load() {
  const r = await fetch('/api/spools');
  spoolsData = await r.json();
  
  const sr = await fetch('/api/stats');
  const stats = await sr.json();
  document.getElementById('stat-total').textContent = stats.total_spools;
  document.getElementById('stat-low').textContent = stats.low_spools;
  document.getElementById('total-spools').textContent = stats.total_spools;
  
  const pr = await fetch('/api/pending');
  const pending = await pr.json();
  
  const psec = document.getElementById('pending-section');
  if (pending.length) {
    psec.style.display = 'block';
    psec.innerHTML = `<div class="card"><h3 style="margin-bottom:1rem">⚠️ Neue Spulen erkannt</h3>
      ${pending.map(p => `<div class="pending-card">
          <div class="pending-info">
            <div class="pending-uid">${p.uid}</div>
            <div class="pending-weight">${p.last_weight ? p.last_weight.toFixed(0) + 'g' : 'Kein Gewicht'}</div>
          </div>
          <div class="pending-actions">
            <button class="btn btn-primary" onclick="openModal('${p.uid}')">Einrichten</button>
            <button class="btn btn-secondary" onclick="dismissPending(event,'${p.uid}')">✕</button>
          </div>
        </div>
      `).join('')}
      </div>`;
  } else {
    psec.style.display = 'none';
  }
  
  renderSpools();
}

async function dismissPending(e, uid) {
  e.stopPropagation();
  await fetch(`/api/pending/${uid}`, {method:'DELETE'});
  load();
}

function sortSpools(col) {
  if (sortColumn === col) {
    sortAsc = !sortAsc;
  } else {
    sortColumn = col;
    sortAsc = true;
  }
  // Update sort icons
  document.querySelectorAll('.sort-icon').forEach(el => {
    el.classList.remove('active');
    el.textContent = '↕';
  });
  const icon = document.getElementById(`sort-${col}`);
  if (icon) {
    icon.classList.add('active');
    icon.textContent = sortAsc ? '↑' : '↓';
  }
  renderSpools();
}

function renderSpools() {
  const tbody = document.getElementById('tbody');
  if (!spoolsData.length) {
    tbody.innerHTML = `<tr><td colspan="11"><div class="empty-state"><div style="font-size:2rem">⚖</div><p>Noch keine Spulen registriert.<br>Spule auf die Waage legen oder manuell hinzufügen.</p></div></td></tr>`;
    return;
  }

  const sorted = [...spoolsData].sort((a, b) => {
    let va = a[sortColumn], vb = b[sortColumn];
    if (va === null || va === undefined) va = sortAsc ? Infinity : -Infinity;
    if (vb === null || vb === undefined) vb = sortAsc ? Infinity : -Infinity;
    if (typeof va === 'string') return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
    return sortAsc ? va - vb : vb - va;
  });

  tbody.innerHTML = sorted.map(s => {
    const mat = s.material||'';
    const cls = ['PLA','PETG','ASA','ABS'].includes(mat) ? `badge-${mat}` : 'badge-other';
    const pct = s.remaining_percent;
    const pc  = pct===null?'':pct<20?'#ef4444':pct<50?'#f59e0b':'#22c55e';
    const bar = pct!==null
      ? `<div class="progress-wrap"><div class="progress-bg"><div class="progress-fill" style="width:${pct}%;background:${pc}"></div></div><span class="pct-text">${pct}%</span></div>`: '<span style="color:var(--muted);font-size:.8rem">—</span>';
    const w   = s.last_weight ? `${s.last_weight.toFixed(0)} g` : '—';
    const ts  = s.updated_at ? s.updated_at.slice(0,16).replace('T',' ') : '—';
    const brand = s.brand ? `<div style="font-size:0.7rem;color:var(--muted);margin-top:2px">${s.brand}</div>` : '';
    const bed = s.bed_temp || 60;
    const noz = s.nozzle_temp || 210;
    const nfc = s.nfc_synced ? `<span class="nfc-indicator">✓</span>` : `<span class="nfc-indicator pending">⏳</span>`;
    const price = s.price_per_kg ? `${s.price_per_kg.toFixed(1)}€` : '—';
    const storage = s.storage_location ? `<span class="storage-tag">📍 ${s.storage_location}</span>` : '<span style="color:var(--muted);font-size:.8rem">—</span>';

    return `<tr>
      <td><span class="uid">${s.uid}</span>${brand}</td>
      <td><span class="badge ${cls}">${mat}</span></td>
      <td>${s.color||'—'}</td>
      <td><strong>${price}</strong></td>
      <td style="font-family:'JetBrains Mono',monospace;font-size:.8rem">${w}</td>
      <td>${bar}</td>
      <td><span class="temp-tag">🛏${bed}°</span><span class="temp-tag">🔥${noz}°</span></td>
      <td>${storage}</td>
      <td>${nfc}</td>
      <td style="font-size:.75rem;color:var(--muted)">${ts}</td>
      <td><div class="actions">
        <button class="btn-icon" onclick="openModal('${s.uid}')" title="Bearbeiten">✏</button>
        <button class="btn-icon del" onclick="deleteSpool('${s.uid}')" title="Löschen">🗑</button>
      </div></td>
    </tr>`;
  }).join('');
}

function suggestPrice() {
  const material = document.getElementById('f-material').value;
  const priceInput = document.getElementById('f-price');
  if (materialPrices[material]) {
    const mp = materialPrices[material];
    if (!priceInput.value) priceInput.value = mp.price || mp;
    // Temperaturen vorschlagen wenn Felder noch leer/default
    if (mp.bed_temp) {
      const bedEl = document.getElementById('f-bed');
      if (!bedEl.value || bedEl.value == 60) bedEl.value = mp.bed_temp;
    }
    if (mp.nozzle_temp) {
      const nozEl = document.getElementById('f-nozzle');
      if (!nozEl.value || nozEl.value == 210) nozEl.value = mp.nozzle_temp;
    }
  }
}

async function openModal(uid=null) {
  editUid = uid;
  document.getElementById('overlay').classList.add('open');
  if (uid) {
    document.getElementById('modal-title').childNodes[0].textContent = 'Spule bearbeiten ';
    document.getElementById('modal-uid').textContent = uid;
    document.getElementById('f-uid').value    = uid;
    document.getElementById('f-uid').disabled = true;
    try {
      const r = await fetch(`/api/spools/${uid}`);
      if (r.ok) {
        const s = await r.json();
        document.getElementById('f-material').value = s.material||'PLA';
        document.getElementById('f-color').value    = s.color||'';
        document.getElementById('f-brand').value    = s.brand||'';
        document.getElementById('f-price').value    = s.price_per_kg||20;
        document.getElementById('f-bed').value      = s.bed_temp||60;
        document.getElementById('f-nozzle').value   = s.nozzle_temp||210;
        document.getElementById('f-empty').value    = s.empty_weight||220;
        document.getElementById('f-full').value     = s.full_weight||1220;
        document.getElementById('f-last').value     = s.last_weight||'';
        document.getElementById('f-storage').value       = s.storage_location||'';
        document.getElementById('f-display-color').value = s.display_color||'';
        document.getElementById('f-order-number').value  = s.order_number||'';
        document.getElementById('f-notes').value          = s.notes||'';
        return;
      }
    } catch{}
    const pr = await fetch('/api/pending');
    const pending = await pr.json();
    const p = pending.find(x => x.uid === uid);
    if (p && p.last_weight) {
      document.getElementById('f-last').value = p.last_weight.toFixed(0);
    }
    document.getElementById('f-material').value = 'PLA';
    document.getElementById('f-color').value    = '';
    document.getElementById('f-brand').value    = '';
    document.getElementById('f-price').value    = materialPrices['PLA'] || 20;
    document.getElementById('f-bed').value      = 60;
    document.getElementById('f-nozzle').value   = 210;
    document.getElementById('f-empty').value    = 220;
    document.getElementById('f-full').value     = 1220;
    document.getElementById('f-storage').value  = '';
  } else {
    document.getElementById('modal-title').childNodes[0].textContent = 'Neue Spule ';
    document.getElementById('modal-uid').textContent = '';
    document.getElementById('f-uid').value    = '';
    document.getElementById('f-uid').disabled = false;
    document.getElementById('f-material').value = 'PLA';
    document.getElementById('f-color').value    = '';
    document.getElementById('f-brand').value    = '';
    document.getElementById('f-price').value    = materialPrices['PLA'] || 20;
    document.getElementById('f-bed').value      = 60;
    document.getElementById('f-nozzle').value   = 210;
    document.getElementById('f-empty').value    = 220;
    document.getElementById('f-full').value     = 1220;
    document.getElementById('f-last').value     = '';
    document.getElementById('f-storage').value      = '';
  document.getElementById('f-display-color').value = '';
  document.getElementById('f-order-number').value  = '';
  document.getElementById('f-notes').value          = '';
  }
}

function closeModal() {
  document.getElementById('overlay').classList.remove('open');
  editUid = null;
}

async function saveSpool() {
  const uid   = document.getElementById('f-uid').value.trim().toUpperCase();
  const mat   = document.getElementById('f-material').value;
  const color = document.getElementById('f-color').value.trim().toUpperCase();
  const brand = document.getElementById('f-brand').value.trim();
  const price = parseFloat(document.getElementById('f-price').value);
  const bed   = parseInt(document.getElementById('f-bed').value)||60;
  const nozzle= parseInt(document.getElementById('f-nozzle').value)||210;
  const empty = parseInt(document.getElementById('f-empty').value)||220;
  const full  = parseInt(document.getElementById('f-full').value)||1220;
  const lastV = document.getElementById('f-last').value;
  const last  = lastV ? parseFloat(lastV) : null;
  const storage     = document.getElementById('f-storage').value;
  const displayColor= document.getElementById('f-display-color').value;
  const orderNumber = document.getElementById('f-order-number').value.trim();
  const notes       = document.getElementById('f-notes').value.trim();
  
  if (!uid)   { toast('UID fehlt!', 'err'); return; }
  if (!color) { toast('Farbe fehlt!', 'err'); return; }
  if (!price || price <= 0) { toast('Preis/kg muss größer als 0 sein!', 'err'); return; }
  
  const body = {uid, material:mat, color, brand, price_per_kg:price, bed_temp:bed, nozzle_temp:nozzle, empty_weight:empty, full_weight:full, storage_location:storage, display_color:displayColor, order_number:orderNumber, notes};
  if (last !== null) body.last_weight = last;
  
  const r = await fetch('/api/set_spool', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const d = await r.json();
  
  if (d.status==='ok') {
    toast(`${uid} gespeichert (${price}€/kg)`,'ok');
    closeModal();
    load();
  } else {
    toast(d.message || 'Fehler beim Speichern','err');
  }
}

async function deleteSpool(uid) {
  if (!confirm(`Spule ${uid} wirklich löschen?`)) return;
  await fetch(`/api/spool/${uid}`, {method:'DELETE'});
  toast(`${uid} gelöscht`,'ok');
  load();
}

async function sendMasterCommand(cmd) {
  try {
    const r = await fetch('/api/command/send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({command: cmd})
    });
    const d = await r.json();
    
    if (d.status === 'ok') {
      toast(`${cmd.toUpperCase()} gesendet`, 'ok');
    } else {
      toast('Fehler beim Senden', 'err');
    }
  } catch (e) {
    toast('Netzwerkfehler', 'err');
  }
}

function confirmMasterCommand(cmd) {
  const messages = {
    'reboot': 'Waage wirklich neu starten?',
    'shutdown': 'Waage wirklich herunterfahren?'
  };
  if (!confirm(messages[cmd])) return;

  if (cmd === 'shutdown') {
    sendMasterCommand(cmd);
    const existing = document.getElementById('shutdown-banner');
    if (existing) existing.remove();
    const banner = document.createElement('div');
    banner.id = 'shutdown-banner';
    banner.style.cssText = 'margin-top:1.25rem;padding:1.25rem;background:rgba(239,68,68,0.1);border:1px solid var(--red);border-radius:10px;text-align:center';
    banner.innerHTML = '<div style="font-size:2rem;margin-bottom:0.5rem">\u23FB</div>' +
      '<div id="sd-title" style="font-weight:700;font-size:1.1rem;margin-bottom:0.4rem">Waage f\u00e4hrt herunter...</div>' +
      '<div id="sd-msg" style="color:var(--muted);font-size:0.875rem">Bitte warten \u2014 <span id="sd-count">15</span> Sekunden</div>';
    document.querySelector('#tab-master .card').appendChild(banner);
    let secs = 15;
    const interval = setInterval(() => {
      secs--;
      const el = document.getElementById('sd-count');
      if (el) el.textContent = secs;
      if (secs <= 0) {
        clearInterval(interval);
        banner.style.background = 'rgba(34,197,94,0.1)';
        banner.style.borderColor = 'var(--green)';
        document.getElementById('sd-title').textContent = '\u2705 Jetzt sicher ausstecken!';
        document.getElementById('sd-msg').textContent = 'Die Waage ist vollst\u00e4ndig heruntergefahren.';
      }
    }, 1000);
  } else {
    sendMasterCommand(cmd);
  }
}

// ========== G-CODE PARSER ==========

let gcodeData = null;

function parseGCode(input) {
  const file = input.files[0];
  if (!file) return;

  const HEADER = 524288; // 512KB header - past thumbnails+polygons to metadata
  const TAIL   = 131072; // 128KB tail for TIME_ELAPSED (last value = total)

  const reader1 = new FileReader();
  reader1.onload = function(e) {
    var headerText = e.target.result;
    var startTail = Math.max(0, file.size - TAIL);
    var blobTail = file.slice(startTail, file.size);
    var reader2 = new FileReader();
    reader2.onload = function(e2) {
      var tailText = e2.target.result;
      gcodeData = extractGCodeData(headerText, tailText, file.name);
      showGCodeData(gcodeData, file.name);
    };
    reader2.readAsText(blobTail);
  };
  reader1.readAsText(file.slice(0, HEADER));
}


function extractGCodeData(headerText, tailText, filename) {
  const data = {
    print_time_seconds: null,
    filament_weights: [],
    filament_total_g: null,
    filament_total_m: null,
    filament_cost: null,
    color_changes: null,
  };

  var NL = String.fromCharCode(10);

  // --- Parse TIME_ELAPSED from TAIL (last value = total print time) ---
  // Also scan tail for Creality metadata (may appear after huge polygon block)
  var tailLines = tailText.split(NL);
  var lastTimeElapsed = null;
  var colorChanges = 0;
  for (var ti = 0; ti < tailLines.length; ti++) {
    var tl = tailLines[ti].trim();
    if (tl.indexOf(";TIME_ELAPSED:") >= 0) {
      var tem = tl.match(/TIME_ELAPSED:([[0-9].]+)/);
      if (tem) lastTimeElapsed = parseFloat(tem[1]);
    }
    if (tl.length === 2 && tl.charAt(0) === "T" && tl.charAt(1) >= "0" && tl.charAt(1) <= "9") {
      colorChanges++;
    }
    // Creality metadata also in tail
    if (tl.charAt(0) === ";" ) {
      var tb = tl.slice(1).trim();
      if (tb.indexOf("Druckzeit:") >= 0) {
        var tdtm = tb.match(/Druckzeit:([\dh]+m)/);
        if (tdtm && !lastTimeElapsed) data.print_time_seconds = parseTimeToSeconds(tdtm[1]);
        var twtm = tb.match(/Filament Wt:([\d.,]+)\s*g/);
        if (twtm && !data.filament_total_g) data.filament_total_g = parseFloat(twtm[1].replace(",","."));
      }
      if (tb.indexOf("Filamentlaenge:") >= 0) {
        var tflm = tb.match(/Filamentlaenge:([\d.,]+)\s*m/);
        if (tflm && !data.filament_total_m) data.filament_total_m = parseFloat(tflm[1].replace(",","."));
        var tfcm = tb.match(/Filamentkosten:([\d.,]+)/);
        if (tfcm && !data.filament_cost) data.filament_cost = parseFloat(tfcm[1].replace(",","."));
      }
    }
  }


  // --- Parse filament data from HEADER ---
  var headerLines = headerText.split(NL);
  for (var hi = 0; hi < headerLines.length; hi++) {
    var line = headerLines[hi];
    if (line.length === 0) continue;
    var l = line.trim();
    if (l.charAt(0) !== ";") continue;
    var body = l.slice(1).trim();

    // Creality Print specific: "; Druckzeit:3h13m  Filament Wt:101,97 g"
    if (body.indexOf("Druckzeit:") >= 0) {
      var dtm = body.match(/Druckzeit:([\dh]+m)/);
      if (dtm && !lastTimeElapsed) data.print_time_seconds = parseTimeToSeconds(dtm[1]);
      var wtm = body.match(/Filament Wt:([\d.,]+)\s*g/);
      if (wtm) data.filament_total_g = parseFloat(wtm[1].replace(",","."));
    }
    // "; Filamentlaenge:33,38 m  Filamentkosten:1,43"
    if (body.indexOf("Filamentlaenge:") >= 0) {
      var flm = body.match(/Filamentlaenge:([\d.,]+)\s*m/);
      if (flm) data.filament_total_m = parseFloat(flm[1].replace(",","."));
      var fcm = body.match(/Filamentkosten:([\d.,]+)/);
      if (fcm) data.filament_cost = parseFloat(fcm[1].replace(",","."));
    }

    // Standard time comments in header
    if (!data.print_time_seconds) {
      var tkeys = ["estimated printing time", "print time", "Druckzeit"];
      for (var ki = 0; ki < tkeys.length; ki++) {
        if (body.toLowerCase().indexOf(tkeys[ki].toLowerCase()) >= 0) {
          var idx3 = body.toLowerCase().indexOf(tkeys[ki].toLowerCase());
          var rest = body.slice(idx3 + tkeys[ki].length);
          var nm = rest.match(/[=:\s]+([\dh m:]+)/);
          if (nm) { data.print_time_seconds = parseTimeToSeconds(nm[1].trim()); break; }
        }
      }
    }

    // Filament total weight: "; filament used [g] = 101.97" or similar
    if (!data.filament_total_g) {
      var wkeys = ["filament used [g]", "Filament Wt", "filament_weight", "total filament used"];
      for (var wi = 0; wi < wkeys.length; wi++) {
        var wb = body.toLowerCase();
        if (wb.indexOf(wkeys[wi].toLowerCase()) >= 0 && wb.indexOf("[mm]") < 0) {
          var r2 = body.slice(wb.indexOf(wkeys[wi].toLowerCase()) + wkeys[wi].length);
          var wm = r2.match(/[=:\s]+([\d.]+)/);
          if (wm) { data.filament_total_g = parseFloat(wm[1]); break; }
        }
      }
    }

    // Filament total length: "; filament used [mm] = 33380"
    if (!data.filament_total_m) {
      var lkeys = ["filament used [mm]", "total filament length", "filament length"];
      for (var li2 = 0; li2 < lkeys.length; li2++) {
        var lb = body.toLowerCase();
        if (lb.indexOf(lkeys[li2].toLowerCase()) >= 0) {
          var r3 = body.slice(lb.indexOf(lkeys[li2].toLowerCase()) + lkeys[li2].length);
          var lm = r3.match(/[=:\s]+([\d.]+)/);
          if (lm) { data.filament_total_m = parseFloat(lm[1]) / 1000.0; break; }
        }
      }
    }

    // Cost
    if (!data.filament_cost) {
      var ckeys = ["Filamentkosten", "filament cost", "Kosten"];
      for (var ci = 0; ci < ckeys.length; ci++) {
        var cb = body.toLowerCase();
        if (cb.indexOf(ckeys[ci].toLowerCase()) >= 0) {
          var r4 = body.slice(cb.indexOf(ckeys[ci].toLowerCase()) + ckeys[ci].length);
          var cm = r4.match(/[=:\s]+([\d.,]+)/);
          if (cm) { data.filament_cost = parseFloat(cm[1].replace(",",".")); break; }
        }
      }
    }

    // Color change count in header comments
    if (data.color_changes === null) {
      var cckeys = ["Filamentwechselzeiten", "total filament change", "color change"];
      for (var cci = 0; cci < cckeys.length; cci++) {
        var ccb = body.toLowerCase();
        if (ccb.indexOf(cckeys[cci].toLowerCase()) >= 0) {
          var r5 = body.slice(ccb.indexOf(cckeys[cci].toLowerCase()) + cckeys[cci].length);
          var ccm = r5.match(/[=:\s]+(\d+)/);
          if (ccm) { data.color_changes = parseInt(ccm[1]); break; }
        }
      }
    }

    // Per-filament weights
    var fg = body.match(/filament used \[g\]\s+(\d+)\s*=\s*([\d.]+)/i);
    if (fg) data.filament_weights.push({ index: parseInt(fg[1]), weight_g: parseFloat(fg[2]) });
    var ft = body.match(/Filament\s+T?(\d+)\s*[=:]\s*([\d.]+)\s*g/i);
    if (ft) data.filament_weights.push({ index: parseInt(ft[1]), weight_g: parseFloat(ft[2]) });
  }

  // TIME_ELAPSED from tail = highest priority (most accurate)
  if (lastTimeElapsed && lastTimeElapsed > 0) {
    data.print_time_seconds = Math.round(lastTimeElapsed);
  }

  // Filename fallback for time: "3h13m"
  if (!data.print_time_seconds && filename) {
    var fnm = filename.match(/(\d+)h(\d+)m/i);
    if (fnm) data.print_time_seconds = parseInt(fnm[1]) * 3600 + parseInt(fnm[2]) * 60;
  }

  if (data.filament_weights.length === 0 && data.filament_total_g) {
    data.filament_weights.push({ index: 0, weight_g: data.filament_total_g });
  }

  return data;
}

function parseTimeToSeconds(str) {
  str = (str || '').trim();
  let m;
  m = str.match(/(\d+)h\s*(\d+)/i);
  if (m) return parseInt(m[1])*3600 + parseInt(m[2]||0)*60;
  m = str.match(/(\d+):(\d+):(\d+)/);
  if (m) return parseInt(m[1])*3600 + parseInt(m[2])*60 + parseInt(m[3]);
  m = str.match(/(\d+):(\d+)/);
  if (m) return parseInt(m[1])*3600 + parseInt(m[2])*60;
  const n = parseFloat(str);
  return isNaN(n) ? null : n;
}
function parseTimeToSeconds(str) {
  str = str.trim();
  let m;
  if ((m = str.match(/(\d+)h\s*(\d+)m?\s*(\d*)/i))) {
    return parseInt(m[1])*3600 + parseInt(m[2]||0)*60 + parseInt(m[3]||0);
  }
  if ((m = str.match(/(\d+):(\d+):(\d+)/))) {
    return parseInt(m[1])*3600 + parseInt(m[2])*60 + parseInt(m[3]);
  }
  if ((m = str.match(/(\d+):(\d+)/))) {
    return parseInt(m[1])*3600 + parseInt(m[2])*60;
  }
  const n = parseFloat(str);
  return isNaN(n) ? null : n;
}

function formatSeconds(sec) {
  if (!sec) return '—';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return `${h}h ${m}m`;
}

function showGCodeData(data, filename) {
  const box = document.getElementById('gcode-import-box');
  const parsed = document.getElementById('gcode-parsed-data');
  const stats = document.getElementById('gcode-stats');
  box.classList.add('has-data');
  parsed.style.display = 'block';
  const timeH = data.print_time_seconds ? (data.print_time_seconds / 3600).toFixed(2) : null;
  let html = '';
  if (data.print_time_seconds) {
    html += '<div class="gcode-stat"><div class="gcode-stat-value">&#9201;&#65039; ' + formatSeconds(data.print_time_seconds) + '</div><div class="gcode-stat-label">Druckzeit (' + timeH + 'h)</div></div>';
  }
  if (data.filament_total_g) {
    html += '<div class="gcode-stat"><div class="gcode-stat-value">' + data.filament_total_g.toFixed(1) + 'g</div><div class="gcode-stat-label">Filament gesamt</div></div>';
  }
  if (data.filament_total_m) {
    html += '<div class="gcode-stat"><div class="gcode-stat-value">' + data.filament_total_m.toFixed(2) + 'm</div><div class="gcode-stat-label">Laenge gesamt</div></div>';
  }
  if (data.filament_cost) {
    html += '<div class="gcode-stat"><div class="gcode-stat-value">' + data.filament_cost.toFixed(2) + ' EUR</div><div class="gcode-stat-label">Slicer-Kosten (Ref.)</div></div>';
  }
  if (data.color_changes !== null) {
    html += '<div class="gcode-stat"><div class="gcode-stat-value">' + data.color_changes + 'x</div><div class="gcode-stat-label">Filamentwechsel</div></div>';
  }
  if (data.filament_weights.length > 1) {
    data.filament_weights.forEach(function(fw) {
      html += '<div class="gcode-stat"><div class="gcode-stat-value" style="color:var(--green)">' + fw.weight_g.toFixed(1) + 'g</div><div class="gcode-stat-label">Farbe ' + (fw.index + 1) + '</div></div>';
    });
  }
  // Manual input section for values not found in G-Code
  var manualHtml = '<div style="margin-top:1rem;padding-top:0.75rem;border-top:1px solid var(--border)">'
    + '<div style="font-size:0.8rem;color:var(--orange);margin-bottom:0.75rem">&#9888;&#65039; Nicht alle Werte konnten automatisch ausgelesen werden &ndash; <strong>Werte nicht bindend</strong>, bitte pr&uuml;fen und erg&auml;nzen:</div>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0.75rem">'
    + '<div>'
    + '<div style="font-size:0.75rem;color:var(--muted);margin-bottom:0.3rem">&#9201; Druckzeit (Std.)</div>'
    + '<input type="number" id="gcode-manual-time" step="0.01" min="0" style="width:100%;padding:0.5rem;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--fg);font-size:0.9rem" value="' + (timeH || '') + '" placeholder="z.B. 3.22">'
    + '</div>'
    + '<div>'
    + '<div style="font-size:0.75rem;color:var(--muted);margin-bottom:0.3rem">&#128024; Gewicht gesamt (g)</div>'
    + '<input type="number" id="gcode-manual-weight" step="0.1" min="0" style="width:100%;padding:0.5rem;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--fg);font-size:0.9rem" value="' + (data.filament_total_g ? data.filament_total_g.toFixed(1) : '') + '" placeholder="z.B. 101.97">'
    + '</div>'
    + '<div>'
    + '<div style="font-size:0.75rem;color:var(--muted);margin-bottom:0.3rem">&#128260; Filamentwechsel</div>'
    + '<input type="number" id="gcode-manual-changes" step="1" min="0" style="width:100%;padding:0.5rem;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--fg);font-size:0.9rem" value="' + (data.color_changes !== null ? data.color_changes : '') + '" placeholder="z.B. 17">'
    + '</div>'
    + '</div>'
    + '</div>';

  stats.innerHTML = html + manualHtml;
  const fileLabel = box.querySelector('div > div > div:first-child');
  if (fileLabel) fileLabel.textContent = filename;
}



// ========== ROLLEN-VERWALTUNG ==========

let rollCount = 0;
let spoolsCache = [];

function getRollsHtml(id, spools) {
  const opts = spools.map(s => {
    const price = s.price_per_kg ? ' (' + s.price_per_kg.toFixed(1) + '€/kg)' : '';
    return '<option value="' + s.uid + '">' + s.uid + ' – ' + s.material + ' ' + s.color + price + '</option>';
  }).join('');
  const isFirst = id === 0;
  const label = isFirst ? '🟢 Hauptrolle' : '🔵 Rolle ' + (id + 1);
  const defaultPct = isFirst ? 100 : 0;
  const deleteBtn = isFirst
    ? '<div class="roll-label">&nbsp;</div><div style="height:42px"></div>'
    : '<div class="roll-label">&nbsp;</div><button type="button" onclick="removeRoll(' + id + ')" style="height:42px;padding:0 0.75rem;background:var(--red);border:none;border-radius:8px;color:white;cursor:pointer;font-size:1rem">✕</button>';

  return '<div class="roll-row" id="roll-row-' + id + '">' +
    '<div>' +
      '<div class="roll-label">' + label + '</div>' +
      '<select id="roll-spool-' + id + '" style="width:100%;padding:0.6rem;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--fg)">' +
        '<option value="">Bitte wählen...</option>' + opts +
      '</select>' +
    '</div>' +
    '<div>' +
      '<div class="roll-label">Gewicht Slicer (g)</div>' +
      '<input type="number" id="roll-weight-' + id + '" value="50" step="1" min="0" ' +
        'style="width:100%;padding:0.6rem;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--fg)">' +
    '</div>' +
    '<div>' +
      '<div class="roll-label">Anteil (%)</div>' +
      '<input type="number" id="roll-pct-' + id + '" value="' + defaultPct + '" step="1" min="0" max="100" ' +
        'style="width:100%;padding:0.6rem;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--fg)" ' +
        'title="Prozentualer Anteil dieser Farbe am Druck">' +
    '</div>' +
    '<div>' + deleteBtn + '</div>' +
  '</div>';
}



function getRolls() {
  const rows = document.getElementById('rolls-container').querySelectorAll('.roll-row');
  const rolls = [];
  rows.forEach(row => {
    const id = row.id.replace('roll-row-', '');
    const uid    = document.getElementById(`roll-spool-${id}`)?.value;
    const weight = parseFloat(document.getElementById(`roll-weight-${id}`)?.value) || 0;
    const pct    = parseFloat(document.getElementById(`roll-pct-${id}`)?.value) || 0;
    if (uid) rolls.push({ uid, weight_grams: weight, percent: pct });
  });
  return rolls;
}

// ========== KOSTENRECHNER FUNCTIONS ==========



function updateMaterialInfo() {
  // Nichts zu tun, Preis kommt automatisch aus Spule
}



function openSaveCalcModal() {
  document.getElementById('save-calc-overlay').classList.add('open');
  document.getElementById('save-calc-name').value = '';
  document.getElementById('save-calc-desc').value = '';
  document.getElementById('save-calc-status').value = 'planned';
  document.getElementById('save-calc-date').value = new Date().toISOString().split('T')[0];
}



async function loadCalculations() {
  const r = await fetch('/api/cost/calculations');
  const calcs = await r.json();
  
  document.getElementById('total-calcs').textContent = calcs.length;
  
  const tbody = document.getElementById('calculations-tbody');
  
  if (!calcs.length) {
    tbody.innerHTML = `<tr><td colspan="9"><div class="empty-state"><div style="font-size:2rem">📊</div><p>Noch keine Kalkulationen gespeichert.<br>Berechne Kosten und speichere sie für später.</p></div></td></tr>`;
    return;
  }
  
  tbody.innerHTML = calcs.map(c => {
    const matInfo = c.material ? `${c.material} ${c.color}` : '—';
    const brand = c.brand ? `<div style="font-size:0.7rem;color:var(--muted)">${c.brand}</div>` : '';
    const desc = c.description ? `<div style="font-size:0.75rem;color:var(--muted);margin-top:2px">${c.description}</div>` : '';
    
    const slicerInfo = `${c.slicer_weight_grams}g / ${c.slicer_time_hours}h`;
    
    let actualInfo = '—';
    if (c.actual_weight_grams && c.actual_time_hours) {
      const wDelta = c.weight_delta_percent > 0 ? `+${c.weight_delta_percent.toFixed(1)}%` : `${c.weight_delta_percent.toFixed(1)}%`;
      const tDelta = c.time_delta_percent > 0 ? `+${c.time_delta_percent.toFixed(1)}%` : `${c.time_delta_percent.toFixed(1)}%`;
      const wClass = c.weight_delta_percent > 0 ? 'delta-positive' : 'delta-negative';
      const tClass = c.time_delta_percent > 0 ? 'delta-positive' : 'delta-negative';
      
      actualInfo = `${c.actual_weight_grams}g (<span class="${wClass}">${wDelta}</span>) /
        ${c.actual_time_hours}h (<span class="${tClass}">${tDelta}</span>)
      `;
    }
    
    const statusBadge = `<span class="badge badge-${c.status}">${c.status === 'planned' ? 'Geplant' : c.status === 'printing' ? 'Wird gedruckt' : 'Abgeschlossen'}</span>`;
    
    return `<tr>
      <td><strong>${c.name}</strong>${desc}</td>
      <td>${matInfo}${brand}</td>
      <td style="font-size:0.85rem">${slicerInfo}</td>
      <td style="font-size:0.85rem">${actualInfo}</td>
      <td>${c.total_cost.toFixed(2)} €</td>
      <td><strong style="color:var(--green)">${c.selling_price.toFixed(2)} €</strong></td>
      <td>${statusBadge}</td>
      <td style="font-size:0.75rem;color:var(--muted)">${c.created_at.slice(0,16).replace('T',' ')}</td>
      <td>
        <div class="actions">
          <button class="btn-icon" onclick="openUpdateCalcModal(${c.id})" title="Tatsächliche Werte nachtragen">📝</button>
          <button class="btn-icon del" onclick="deleteCalculation(${c.id})" title="Löschen">🗑</button>
        </div>
      </td>
    </tr>`;
  }).join('');
}

function openUpdateCalcModal(calcId) {
  currentUpdateCalcId = calcId;
  document.getElementById('update-calc-overlay').classList.add('open');
  document.getElementById('update-actual-weight').value = '';
  document.getElementById('update-actual-time').value = '';
  document.getElementById('update-calc-status').value = 'completed';
}

function closeUpdateCalcModal() {
  document.getElementById('update-calc-overlay').classList.remove('open');
  currentUpdateCalcId = null;
}

async function updateCalculation() {
  const actualWeight = parseFloat(document.getElementById('update-actual-weight').value);
  const actualTime = parseFloat(document.getElementById('update-actual-time').value);
  const status = document.getElementById('update-calc-status').value;
  
  if (!actualWeight || !actualTime) {
    toast('Bitte beide Werte eingeben', 'err');
    return;
  }
  
  const body = {
    actual_weight_grams: actualWeight,
    actual_time_hours: actualTime,
    status
  };
  
  const r = await fetch(`/api/cost/calculations/${currentUpdateCalcId}`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  
  const result = await r.json();
  
  if (result.status === 'ok') {
    toast('Werte gespeichert - System lernt daraus! 🎯', 'ok');
    closeUpdateCalcModal();
    loadCalculations();
    loadStatistics();
  } else {
    toast('Fehler beim Speichern', 'err');
  }
}

async function deleteCalculation(id) {
  if (!confirm('Kalkulation wirklich löschen?')) return;
  await fetch(`/api/cost/calculations/${id}`, {method:'DELETE'});
  toast('Kalkulation gelöscht', 'ok');
  loadCalculations();
  loadStatistics();
}

async function loadStatistics() {
  const r = await fetch('/api/cost/statistics');
  const stats = await r.json();
  
  document.getElementById('stats-total-calcs').textContent = stats.total_calculations;
  document.getElementById('stats-completed').textContent = stats.completed_prints;
  
  const weightDev = stats.avg_weight_deviation_percent;
  const timeDev = stats.avg_time_deviation_percent;
  
  document.getElementById('stats-weight-dev').textContent = 
    weightDev > 0 ? `+${weightDev}%` : `${weightDev}%`;
  document.getElementById('stats-time-dev').textContent = 
    timeDev > 0 ? `+${timeDev}%` : `${timeDev}%`;
}

async function loadSettings() {
  loadNetworkSettings();
  loadCompanySettings();
  const r = await fetch('/api/cost/settings');
  const settings = await r.json();
  
  document.getElementById('set-power-cost').value = settings.power_cost_per_kwh || 0.35;
  document.getElementById('set-power-watts').value = settings.printer_power_watts || 150;
  document.getElementById('set-printer-price').value = settings.printer_purchase_price || 300;
  document.getElementById('set-lifetime').value = settings.printer_lifetime_hours || 5000;
  document.getElementById('set-failure').value = settings.failure_rate_percent || 5;
  document.getElementById('set-margin').value = settings.default_profit_margin || 30;
  document.getElementById('set-printer-labor').value = settings.printer_labor_cost_per_hour || 0;
  document.getElementById('set-pre-post-labor').value = settings.pre_post_labor_cost_per_hour || 0;
  // Standardwert für Vor-/Nachbearbeitungszeit im Kostenrechner vorbelegen
  const prePostEl = document.getElementById('calc-pre-post-time');
  if (prePostEl && !prePostEl.value) prePostEl.value = settings.pre_post_time_minutes || 0;
  
  // Material prices
  const pr = await fetch('/api/cost/material-prices');
  const prices = await pr.json();
  
  const container = document.getElementById('material-prices');
  container.innerHTML = prices.map(p => `<div style="display:grid;grid-template-columns:100px 1fr auto;align-items:center;gap:0;margin-bottom:0.5rem;background:var(--hover);border-radius:8px;overflow:hidden">
      <div style="padding:0.75rem 1rem;background:var(--card);font-weight:700;font-size:0.95rem;border-right:2px solid var(--border);align-self:stretch;display:flex;align-items:center">
        ${p.material}
      </div>
      <div style="display:flex;align-items:center;gap:1rem;padding:0.75rem 1rem;flex-wrap:wrap">
        <div style="display:flex;align-items:center;gap:0.4rem">
          <span style="color:var(--muted);font-size:0.8rem">€/kg</span>
          <input type="number" value="${p.price_per_kg}" step="0.1" min="0"
            id="price-${p.material}"
            style="width:85px;padding:0.4rem 0.5rem;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--fg)">
        </div>
        <div style="display:flex;align-items:center;gap:0.4rem">
          <span style="color:var(--muted);font-size:0.8rem">🛏 Bett °C</span>
          <input type="number" value="${p.bed_temp||60}" step="5" min="0" max="150"
            id="bed-${p.material}"
            style="width:70px;padding:0.4rem 0.5rem;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--fg)">
        </div>
        <div style="display:flex;align-items:center;gap:0.4rem">
          <span style="color:var(--muted);font-size:0.8rem">🔥 Düse °C</span>
          <input type="number" value="${p.nozzle_temp||210}" step="5" min="100" max="350"
            id="nozzle-${p.material}"
            style="width:70px;padding:0.4rem 0.5rem;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--fg)">
        </div>
      </div>
      <div style="padding:0.75rem 1rem">
        <button class="btn btn-primary" onclick="updateMaterialPrice('${p.material}')" style="padding:0.5rem 1rem;white-space:nowrap">💾 Speichern</button>
      </div>
    </div>
  `).join('');
}

async function saveSettings() {
  const body = {
    power_cost_per_kwh: parseFloat(document.getElementById('set-power-cost').value),
    printer_power_watts: parseFloat(document.getElementById('set-power-watts').value),
    printer_purchase_price: parseFloat(document.getElementById('set-printer-price').value),
    printer_lifetime_hours: parseFloat(document.getElementById('set-lifetime').value),
    failure_rate_percent: parseFloat(document.getElementById('set-failure').value),
    default_profit_margin: parseFloat(document.getElementById('set-margin').value),
    labor_cost_per_hour: 0,
    printer_labor_cost_per_hour: parseFloat(document.getElementById('set-printer-labor').value),
    pre_post_labor_cost_per_hour: parseFloat(document.getElementById('set-pre-post-labor').value),
    pre_post_time_minutes: 0
  };
  
  const r = await fetch('/api/cost/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  
  const result = await r.json();
  
  if (result.status === 'ok') {
    toast('Einstellungen gespeichert', 'ok');
  } else {
    toast('Fehler beim Speichern', 'err');
  }
}

async function updateMaterialPrice(material) {
  const price  = parseFloat(document.getElementById(`price-${material}`).value);
  const bed    = parseInt(document.getElementById(`bed-${material}`).value) || 60;
  const nozzle = parseInt(document.getElementById(`nozzle-${material}`).value) || 210;
  
  const r = await fetch('/api/cost/material-prices', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({material, price_per_kg: price, bed_temp: bed, nozzle_temp: nozzle})
  });
  
  const result = await r.json();
  
  if (result.status === 'ok') {
    toast(`${material} gespeichert (${price}€/kg, Bett:${bed}°, Düse:${nozzle}°)`, 'ok');
    await loadMaterialPrices();
  } else {
    toast('Fehler beim Speichern', 'err');
  }
}

// ========== UTILITY FUNCTIONS ==========

function toast(msg, type='ok') {
  const el = document.getElementById('toast');
  el.textContent = (type==='ok'?'✓ ':'✕ ') + msg;
  el.className = `toast ${type} show`;
  setTimeout(()=>el.classList.remove('show'), 4000);
}

async function updateStatus() {
  try {
    const r = await fetch('/api/ping');
    const d = await r.json();
    document.getElementById('srv-dot').style.background = 'var(--green)';
    document.getElementById('srv-time').textContent = d.time.slice(0,19).replace('T',' ');
  } catch {
    document.getElementById('srv-dot').style.background = 'var(--red)';
    document.getElementById('srv-time').textContent = 'offline';
  }
}

async function updatePrinterStatus() {
  try {
    const r = await fetch('/api/printer/status');
    const d = await r.json();
    const dot  = document.getElementById('printer-dot');
    const span = document.getElementById('printer-status');
    const link = document.getElementById('printer-link');
    if (d.online) {
      dot.style.background = 'var(--green)';
      span.textContent = 'online';
      link.style.color = 'var(--green)';
    } else {
      dot.style.background = 'var(--red)';
      span.textContent = 'offline';
      link.style.color = 'var(--muted)';
    }
  } catch {
    document.getElementById('printer-dot').style.background = 'var(--muted)';
    document.getElementById('printer-status').textContent = '—';
  }
}

async function updateScaleStatus() {
  try {
    const r = await fetch('/api/scale/status');
    const d = await r.json();
    const dot  = document.getElementById('scale-dot');
    const span = document.getElementById('scale-status');
    if (d.online) {
      dot.style.background = 'var(--green)';
      span.textContent = 'online';
      span.style.color = 'var(--green)';
    } else {
      dot.style.background = 'var(--red)';
      span.textContent = 'offline';
      span.style.color = 'var(--red)';
    }
  } catch {
    document.getElementById('scale-dot').style.background = 'var(--muted)';
    document.getElementById('scale-status').textContent = '—';
  }
}

document.getElementById('overlay').addEventListener('click', function(e) {
  if (e.target===this) closeModal();
});

document.getElementById('save-calc-overlay').addEventListener('click', function(e) {
  if (e.target===this) closeSaveCalcModal();
});

document.getElementById('update-calc-overlay').addEventListener('click', function(e) {
  if (e.target===this) closeUpdateCalcModal();
});

document.addEventListener('keydown', e => { 
  if(e.key==='Escape') {
    closeModal();
    closeSaveCalcModal();
    closeUpdateCalcModal();
  }
});

// Initialize
init();

// ===== DRUCKHISTORIE (v2.11) =====
async function loadHistory() {
  try {
    const [histResp, statsResp] = await Promise.all([
      fetch('/api/printer/history?limit=100'),
      fetch('/api/printer/history/stats')
    ]);
    const jobs = await histResp.json();
    const stats = await statsResp.json();
    // Stats
    document.getElementById('hist-total').textContent = stats.total_jobs || 0;
    document.getElementById('hist-filament').textContent = (stats.total_filament_g || 0).toFixed(0) + 'g';
    document.getElementById('hist-hours').textContent = (stats.total_print_hours || 0).toFixed(1) + 'h';
    document.getElementById('hist-cost').textContent = (stats.total_filament_cost || 0).toFixed(2) + '\u20ac';
    // Tabelle
    const tbody = document.getElementById('history-tbody');
    if (!jobs.length) {
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:2rem">Noch keine Eintr\u00e4ge. Kalkulationen mit \"Speichern & Lernen\" abschlie\u00dfen oder Moonraker synchronisieren.</td></tr>';
      return;
    }
    tbody.innerHTML = jobs.map(j => {
      const statusBadge = j.status === 'completed'
        ? '<span class="badge badge-completed">Abgeschlossen</span>'
        : j.status === 'cancelled' ? '<span class="badge" style="background:var(--red);color:#fff">Abgebrochen</span>'
        : '<span class="badge badge-planned">' + j.status + '</span>';
      const durH = j.duration_h ? j.duration_h.toFixed(2) + 'h' : (j.print_duration ? (j.print_duration/3600).toFixed(2) + 'h' : '—');
      const filmG = j.filament_used_g ? j.filament_used_g.toFixed(1) + 'g' : '—';
      const cost = j.filament_cost ? j.filament_cost.toFixed(3) + '\u20ac' : '—';
      const spoolInfo = j.color ? '<span class="badge badge-' + (j.material||'PLA') + '">' + (j.material||'') + '</span> ' + j.color : '<span style="color:var(--muted);font-size:0.8rem">—</span>';
      const source = j.job_id && j.job_id.startsWith('calc_') ? '&#128203; Kalkulation' : '&#127775; Moonraker';
      return '<tr><td>' + (j.start_date||'—') + '</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + (j.filename||'') + '">' + (j.filename||j.job_id||'—') + '</td><td>' + statusBadge + '</td><td>' + durH + '</td><td>' + filmG + '</td><td>' + cost + '</td><td>' + spoolInfo + '</td><td style="font-size:0.8rem;color:var(--muted)">' + source + '</td></tr>';
    }).join('');
  } catch(e) { console.error('loadHistory:', e); }
}

async function syncMoonrakerHistory() {
  try {
    const r = await fetch('/api/printer/history/sync', {method:'POST'});
    const d = await r.json();
    if (d.status === 'ok') { toast('Moonraker: ' + d.synced + ' Jobs synchronisiert', 'ok'); loadHistory(); }
    else toast('Sync-Fehler: ' + (d.message||''), 'err');
  } catch(e) { toast('Drucker nicht erreichbar', 'err'); }
}

// ===== NETZWERK-EINSTELLUNGEN (v2.11) =====
async function loadNetworkSettings() {
  try {
    const r = await fetch('/api/network/settings');
    const d = await r.json();
    document.getElementById('net-printer-ip').value = d.printer_ip || '';
    document.getElementById('net-api-port').value = d.printer_api_port || 7125;
    document.getElementById('net-fluidd-port').value = d.printer_fluidd_port || 4408;
    document.getElementById('net-waage-ip').value = d.waage_ip || '';
    // Drucker-Link in Statusbar dynamisch setzen
    const link = document.getElementById('printer-link');
    if (link && d.printer_ip) {
      link.href = 'http://' + d.printer_ip + ':' + (d.printer_fluidd_port||4408) + '/#/';
    }
  } catch(e) {}
}
async function saveNetworkSettings() {
  const body = {
    printer_ip: document.getElementById('net-printer-ip').value.trim(),
    printer_api_port: parseInt(document.getElementById('net-api-port').value)||7125,
    printer_fluidd_port: parseInt(document.getElementById('net-fluidd-port').value)||4408,
    waage_ip: document.getElementById('net-waage-ip').value.trim()
  };
  const r = await fetch('/api/network/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const d = await r.json();
  if (d.status==='ok') { toast('Netzwerk gespeichert', 'ok'); loadNetworkSettings(); }
  else toast('Fehler beim Speichern', 'err');
}

async function loadCompanySettings() {
  try {
    const r = await fetch('/api/company');
    const d = await r.json();
    document.getElementById('co-name').value    = d.name    || '';
    document.getElementById('co-street').value  = d.street  || '';
    document.getElementById('co-city').value    = d.city    || '';
    document.getElementById('co-phone').value   = d.phone   || '';
    document.getElementById('co-email').value   = d.email   || '';
    document.getElementById('co-website').value = d.website || '';
    document.getElementById('co-taxid').value   = d.tax_id  || '';
    document.getElementById('co-bank').value    = d.bank    || '';
    document.getElementById('co-iban').value    = d.iban    || '';
  } catch(e) {}
}

async function saveCompanySettings() {
  const body = {
    name:    document.getElementById('co-name').value.trim(),
    street:  document.getElementById('co-street').value.trim(),
    city:    document.getElementById('co-city').value.trim(),
    phone:   document.getElementById('co-phone').value.trim(),
    email:   document.getElementById('co-email').value.trim(),
    website: document.getElementById('co-website').value.trim(),
    tax_id:  document.getElementById('co-taxid').value.trim(),
    bank:    document.getElementById('co-bank').value.trim(),
    iban:    document.getElementById('co-iban').value.trim(),
  };
  const r = await fetch('/api/company', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const d = await r.json();
  if (d.status==='ok') toast('Firmendaten gespeichert', 'ok');
  else toast('Fehler', 'err');
} <!-- Neuer Auftrag Modal -->  <div class=\"overlay\" id=\"new-job-overlay\"> <div class=\"modal\"> <div class=\"modal-header\"> <h2>Neuer Auftrag</h2> </div> <div class=\"form-grid\"> <div class=\"form-group\" style=\"grid-column:1/-1\"> <label>Name *</label> <input type=\"text\" id=\"new-job-name\" placeholder=\"z.B. Benchy, Vase, Halter...\"> </div> <div class=\"form-group\" style=\"grid-column:1/-1\"> <label>Beschreibung (optional)</label> <input type=\"text\" id=\"new-job-desc\" placeholder=\"Optionale Details\"> </div> </div> <div style=\"margin-top:1rem;padding:1rem;background:var(--hover);border-radius:8px;border:2px dashed var(--border)\"> <div style=\"font-weight:600;margin-bottom:0.5rem\">📁 .3mf Projektdatei (optional)</div> <div style=\"font-size:0.825rem;color:var(--muted);margin-bottom:0.5rem\">Druckzeit, Gewicht und Vorschaubild werden automatisch ausgelesen</div> <label style=\"cursor:pointer\"><input type=\"file\" accept=\".3mf\" style=\"display:none\" onchange=\"parse3mfFile(this)\"><span class=\"btn btn-secondary\" style=\"padding:0.4rem 0.8rem;font-size:0.85rem\">📁 .3mf wählen</span></label> <div id=\"new-job-3mf-result\"></div> </div> <input type=\"hidden\" id=\"new-job-time\" value=\"0\"> <input type=\"hidden\" id=\"new-job-weight\" value=\"0\"> <input type=\"hidden\" id=\"new-job-thumbnail\" value=\"\"> <div class=\"modal-actions\"> <button class=\"btn btn-secondary\" onclick=\"closeNewJobModal()\">Abbrechen</button> <button class=\"btn btn-primary\" onclick=\"saveNewJob()\">Auftrag anlegen</button> </div> </div> </div>
// ===== PLATTENDETAIL-MODAL (v2.12) =====

let currentPlateId = null;
let plateSpools = [];
let plateFilaments = [];

async function openPlateModal(plateId, orderId) {
  currentPlateId = plateId;
  document.getElementById('plate-overlay').classList.add('open');
  try {
    const r = await fetch('/api/spools');
    plateSpools = await r.json();
  } catch(e) { plateSpools = []; }

  const job = jobsData.find(j => j.id === orderId);
  const plate = job ? job.plates.find(p => p.id === plateId) : null;
  if (!plate) return;

  const titleEl = document.getElementById('plate-modal-title');
  const thumb = plate.thumbnail_b64
    ? '<img src="data:image/png;base64,' + plate.thumbnail_b64 + '" style="height:36px;border-radius:4px;vertical-align:middle;margin-left:10px;border:1px solid var(--border)">'
    : '';
  titleEl.innerHTML = 'Platte ' + plate.plate_number + ' &#8212; ' + (job ? job.name : '') + thumb;

    // G-Code Name anzeigen falls vorhanden
  const gcNameEl = document.getElementById('plate-gcode-name');
  const gcClearEl = document.getElementById('plate-gcode-clear');
  if (gcNameEl) {
    const hasGcode = plate.slicer_time_h > 0 || plate.slicer_weight_g > 0;
    gcNameEl.style.display = hasGcode ? 'inline' : 'none';
    gcNameEl.innerHTML = hasGcode ? '&#10003; Daten geladen' : '';
    if (gcClearEl) gcClearEl.style.display = hasGcode ? 'inline' : 'none';
  }

  // Korrekturfaktoren laden und anzeigen
  try {
    const cfR = await fetch('/api/correction-factors');
    const cf = await cfR.json();
    const cfInfo  = document.getElementById('plate-cf-info');
    const cfLabel = document.getElementById('plate-cf-label');
    const cfCheck = document.getElementById('plate-use-correction');
    if (cfInfo && cf.samples_count >= 3) {
      const wPct = ((cf.weight_factor - 1) * 100).toFixed(1);
      const tPct = ((cf.time_factor   - 1) * 100).toFixed(1);
      const wSign = cf.weight_factor >= 1 ? '+' : '';
      const tSign = cf.time_factor   >= 1 ? '+' : '';
      cfInfo.style.display = 'block';
      cfInfo.style.background = 'rgba(168,85,247,0.12)';
      cfInfo.style.border = '1px solid var(--purple)';
      cfInfo.innerHTML = '&#127919; <strong style="color:var(--purple)">Korrekturfaktoren verfügbar</strong> (' + cf.samples_count + ' Drucke) &mdash; '
        + 'Gewicht: <strong>' + wSign + wPct + '%</strong> &nbsp;|&nbsp; '
        + 'Zeit: <strong>' + tSign + tPct + '%</strong>';
      if (cfLabel) cfLabel.textContent = '(' + cf.samples_count + ' Datenpunkte)';
    } else if (cfInfo) {
      cfInfo.style.display = 'block';
      cfInfo.style.background = 'rgba(115,115,115,0.1)';
      cfInfo.style.border = '1px solid var(--border)';
      cfInfo.innerHTML = '&#9432; Korrekturfaktor noch nicht verfügbar &mdash; mind. 3 abgeschlossene Drucke mit tatsächlichen Werten nötig (aktuell: ' + (cf.samples_count || 0) + '/3)';
      if (cfCheck) cfCheck.disabled = true;
      if (cfLabel) cfLabel.textContent = 'nicht verfügbar';
    }
  } catch(e) {}
  document.getElementById('plate-slicer-weight').value = plate.slicer_weight_g || 0;
  document.getElementById('plate-slicer-time').value = plate.slicer_time_h || 0;
  document.getElementById('plate-margin').value = plate.profit_margin || 30;
  document.getElementById('plate-pre-post').value = plate.pre_post_time_min || 0;
  document.getElementById('plate-status').value = plate.status || 'Offen';
  document.getElementById('plate-failure-notes').value = plate.failure_notes || '';
  document.getElementById('plate-include-costs').checked = plate.include_in_costs !== 0;
  document.getElementById('plate-actual-weight').value = plate.actual_weight_g || '';
  document.getElementById('plate-actual-time').value = plate.actual_time_h || '';
  toggleFailureSection(plate.status);

  plateFilaments = [];
  try { plateFilaments = JSON.parse(plate.filaments || '[]'); } catch(e) {}
  if (!plateFilaments.length && plate.slicer_weight_g > 0) {
    plateFilaments = [{ uid: '', weight_g: plate.slicer_weight_g }];
  }
  renderPlateFilaments();

  if (plate.offer_selling_price > 0) {
    showPlateResult({
      material_cost: 0, power_cost: 0, wear_cost: 0, labor_cost: 0,
      failure_cost: 0, total_cost: plate.offer_total_cost,
      selling_price: plate.offer_selling_price,
      profit_amount: plate.offer_selling_price - plate.offer_total_cost,
      total_weight_g: plate.slicer_weight_g, print_hours: plate.slicer_time_h
    });
  } else {
    document.getElementById('plate-result').style.display = 'none';
  }
}

function closePlateModal() {
  document.getElementById('plate-overlay').classList.remove('open');
  currentPlateId = null;
}

function toggleFailureSection(status) {
  const fs = document.getElementById('plate-failure-section');
  if (fs) fs.style.display = status === 'Fehlschlag' ? 'block' : 'none';
}

function renderPlateFilaments() {
  const container = document.getElementById('plate-filaments');
  const opts = plateSpools.map(s =>
    '<option value="' + s.uid + '">' + s.uid + ' - ' + s.material + ' ' + s.color +
    ' (' + (s.price_per_kg || 0).toFixed(2) + ' EUR/kg)</option>'
  ).join('');

  container.innerHTML = plateFilaments.map((f, i) => `
    <div class="filament-row" style="display:grid;grid-template-columns:1fr 120px auto;gap:0.5rem;align-items:end;margin-bottom:0.5rem;padding:0.5rem;background:var(--hover);border-radius:8px">
      <div class="form-group" style="margin:0">
        <label style="font-size:0.75rem">Spule ${i + 1}</label>
        <select id="pf-uid-${i}" onchange="plateFilaments[${i}].uid=this.value;this.style.border='1px solid var(--border)';this.style.boxShadow=''" style="padding:0.5rem;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--fg);font-size:0.85rem;width:100%">
          <option value="">-- Spule waehlen --</option>${opts}
        </select>
      </div>
      <div class="form-group" style="margin:0">
        <label style="font-size:0.75rem">Gewicht (g)</label>
        <input type="number" id="pf-wt-${i}" value="${f.weight_g || 0}" step="0.1" min="0"
          onchange="plateFilaments[${i}].weight_g=parseFloat(this.value)||0"
          style="padding:0.5rem;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--fg);font-size:0.85rem;width:100%">
      </div>
      <button class="btn-icon del" onclick="removePlateFilament(${i})" title="Entfernen">&#128465;</button>
    </div>`).join('');

  plateFilaments.forEach((f, i) => {
    const sel = document.getElementById('pf-uid-' + i);
    if (sel && f.uid) sel.value = f.uid;
  });
}

function addPlateFilament() {
  const totalWeight = parseFloat(document.getElementById('plate-slicer-weight').value) || 0;
  const used = plateFilaments.reduce((s, f) => s + (f.weight_g || 0), 0);
  plateFilaments.push({ uid: '', weight_g: Math.max(0, Math.round(totalWeight - used)) });
  renderPlateFilaments();
}

function removePlateFilament(idx) {
  plateFilaments.splice(idx, 1);
  renderPlateFilaments();
}

async function calculatePlate() {
  if (!currentPlateId) return;
  const margin  = parseFloat(document.getElementById('plate-margin').value) || 30;
  const prePost = parseFloat(document.getElementById('plate-pre-post').value) || 0;
  const weight  = parseFloat(document.getElementById('plate-slicer-weight').value) || 0;
  if (plateFilaments.length === 1 && !plateFilaments[0].weight_g) {
    plateFilaments[0].weight_g = weight;
  }

  // Validierung: alle Filament-Slots müssen eine Spule haben
  const missingSpools = plateFilaments.filter(function(f) { return !f.uid || f.uid === ''; });
  if (missingSpools.length > 0) {
    // Fehlende Slots rot markieren
    const rows = document.querySelectorAll('#plate-filaments .filament-row');
    rows.forEach(function(row, i) {
      const sel = row.querySelector('select');
      if (sel && (!plateFilaments[i] || !plateFilaments[i].uid || plateFilaments[i].uid === '')) {
        sel.style.border = '2px solid var(--red)';
        sel.style.boxShadow = '0 0 0 3px rgba(239,68,68,0.2)';
      }
    });
    toast(missingSpools.length + ' Spule(n) noch nicht zugewiesen!', 'err');
    return;
  }
  const btn = document.getElementById('plate-calc-btn');
  btn.textContent = 'Berechne...'; btn.disabled = true;
  try {
    const r = await fetch('/api/plates/' + currentPlateId + '/calculate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        filaments: plateFilaments, profit_margin: margin,
        pre_post_time_min: prePost,
        use_correction: document.getElementById('plate-use-correction').checked
      })
    });
    const result = await r.json();
    if (result.error) { toast(result.error, 'err'); return; }
    showPlateResult(result);
    loadJobs();
    toast('Kalkulation gespeichert!', 'ok');
  } catch(e) { toast('Fehler bei Kalkulation', 'err'); }
  finally { btn.textContent = 'Kosten berechnen & speichern'; btn.disabled = false; }
}

function showPlateResult(r) {
  const div = document.getElementById('plate-result');
  div.style.display = 'block';
  const cfBadge = (r.corrected_weight || r.corrected_time)
    ? '<div style="font-size:0.75rem;background:rgba(168,85,247,0.2);border:1px solid var(--purple);border-radius:6px;padding:0.3rem 0.6rem;margin-bottom:0.5rem">'
      + '&#127919; Korrekturfaktor aktiv &mdash; '
      + (r.corrected_weight ? 'Gewicht: <strong>' + r.corrected_weight + 'g</strong> ' : '')
      + (r.corrected_time   ? 'Zeit: <strong>' + r.corrected_time + 'h</strong>' : '')
      + '</div>'
    : '';
  div.innerHTML = `
    <div style="background:linear-gradient(135deg,#1e3a8a,#3b82f6);padding:1.25rem;border-radius:10px;margin-top:1rem">
      ${cfBadge}
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem;margin-bottom:0.75rem">
        <div style="background:rgba(255,255,255,0.12);padding:0.6rem;border-radius:6px;font-size:0.85rem">
          <div style="opacity:0.8">Material</div><strong>${r.material_cost.toFixed(2)} EUR</strong>
        </div>
        <div style="background:rgba(255,255,255,0.12);padding:0.6rem;border-radius:6px;font-size:0.85rem">
          <div style="opacity:0.8">Strom + Verschleiss</div><strong>${((r.power_cost||0)+(r.wear_cost||0)).toFixed(2)} EUR</strong>
        </div>
        <div style="background:rgba(255,255,255,0.12);padding:0.6rem;border-radius:6px;font-size:0.85rem">
          <div style="opacity:0.8">Arbeit</div><strong>${(r.labor_cost||0).toFixed(2)} EUR</strong>
        </div>
        <div style="background:rgba(255,255,255,0.12);padding:0.6rem;border-radius:6px;font-size:0.85rem">
          <div style="opacity:0.8">Fehldruck-Reserve</div><strong>${(r.failure_cost||0).toFixed(2)} EUR</strong>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;padding:0.75rem;background:rgba(255,255,255,0.15);border-radius:8px;margin-bottom:0.5rem">
        <span>Selbstkosten</span><strong>${r.total_cost.toFixed(2)} EUR</strong>
      </div>
      <div style="background:#22c55e;padding:1rem;border-radius:8px;text-align:center">
        <div style="font-size:0.8rem;opacity:0.9">Angebotspreis</div>
        <div style="font-size:2rem;font-weight:700">${r.selling_price.toFixed(2)} EUR</div>
        <div style="font-size:0.8rem;opacity:0.8">Gewinn: ${r.profit_amount.toFixed(2)} EUR</div>
      </div>
    </div>`;
}

async function savePlateStatus() {
  if (!currentPlateId) return;
  const status = document.getElementById('plate-status').value;
  const actualW = parseFloat(document.getElementById('plate-actual-weight').value) || 0;
  const actualT = parseFloat(document.getElementById('plate-actual-time').value) || 0;
  const job = jobsData.find(j => j.plates.some(p => p.id === currentPlateId));
  const plate = job ? job.plates.find(p => p.id === currentPlateId) : {};
  const r = await fetch('/api/plates/' + currentPlateId, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      ...plate, status,
      failure_notes: document.getElementById('plate-failure-notes').value,
      include_in_costs: document.getElementById('plate-include-costs').checked ? 1 : 0,
      actual_weight_g: actualW,
      actual_time_h: actualT,
      filaments: plateFilaments,
      slicer_weight_g: parseFloat(document.getElementById('plate-slicer-weight').value) || 0,
      slicer_time_h: parseFloat(document.getElementById('plate-slicer-time').value) || 0,
      profit_margin: parseFloat(document.getElementById('plate-margin').value) || 30,
      pre_post_time_min: parseFloat(document.getElementById('plate-pre-post').value) || 0
    })
  });
  const result = await r.json();
  closePlateModal();
  loadJobs();
  if (result.correction_updated && result.correction_factors) {
    const cf = result.correction_factors;
    const wPct = ((cf.weight_factor - 1) * 100).toFixed(1);
    const tPct = ((cf.time_factor   - 1) * 100).toFixed(1);
    toast('Gespeichert! 🎯 Korrekturfaktor aktualisiert (' + cf.samples_count + ' Drucke — Gewicht: ' + (cf.weight_factor>=1?'+':'') + wPct + '%, Zeit: ' + (cf.time_factor>=1?'+':'') + tPct + '%)', 'ok');
  } else {
    toast('Platte gespeichert!', 'ok');
  }
}

// ===== AUFTRAeGE (v2.12) =====

let jobsData = [];
let archiveVisible = false;

async function loadJobs() {
  try {
    const r = await fetch('/api/jobs');
    jobsData = await r.json();
    renderJobs();
  } catch(e) { console.error('Jobs laden fehlgeschlagen', e); }
}

function renderJobs() {
  const active = jobsData.filter(j => !['Abgeschlossen','Abgelehnt','Zurueckgestellt'].includes(j.status));
  const archived = jobsData.filter(j => ['Abgeschlossen','Abgelehnt','Zurueckgestellt'].includes(j.status));

  // Stats
  document.getElementById('jobs-stat-total').textContent = jobsData.length;
  document.getElementById('jobs-stat-open').textContent = active.filter(j => j.status === 'Warteschlange' || j.status === 'Angebot').length;
  document.getElementById('jobs-stat-printing').textContent = active.filter(j => j.status === 'In Druck').length;
  document.getElementById('jobs-stat-done').textContent = archived.filter(j => j.status === 'Abgeschlossen').length;

  // Aktive Liste
  const list = document.getElementById('jobs-list');
  if (active.length === 0) {
    list.innerHTML = '<div class="empty-state"><p>Keine aktiven Auftraege. Klicke "+ Neuer Auftrag" um zu starten.</p></div>';
  } else {
    list.innerHTML = active.map(job => renderJobCard(job)).join('');
  }

  // Archiv
  const arch = document.getElementById('jobs-archive');
  arch.innerHTML = archived.length === 0
    ? '<p style="color:var(--muted);font-size:0.875rem">Keine archivierten Auftraege.</p>'
    : archived.map(job => renderJobCard(job, true)).join('');
}

function jobStatusColor(status) {
  const colors = {
    'Angebot': 'var(--muted)',
    'Warteschlange': 'var(--orange)',
    'In Druck': 'var(--accent)',
    'Abgeschlossen': 'var(--green)',
    'Abgelehnt': 'var(--red)',
    'Zurueckgestellt': 'var(--purple)'
  };
  return colors[status] || 'var(--muted)';
}

function renderJobCard(job, archived = false) {
  // Plattenvorschau: Thumbnail + Name + Status + G-Code-Indikator
  const plateBar = job.plates_total > 0
    ? `<div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
        ${job.plates.map(p => {
          const statusColor = p.status === 'Abgeschlossen' ? 'var(--green)' : p.status === 'Fehlschlag' ? 'var(--red)' : p.status === 'In Druck' ? 'var(--accent)' : 'var(--border)';
          const hasGcode   = p.slicer_time_h > 0 || p.slicer_weight_g > 0;
          const hasSpool   = (() => { try { const f = JSON.parse(p.filaments||'[]'); return f.some(x=>x.uid&&x.uid!==''); } catch(e){return false;} })();
          const hasPrice   = p.offer_selling_price > 0;
          // Ampel: Rot=kein GCode, Gelb=GCode aber keine Spule, Grün=komplett
          const gColor = !hasGcode ? 'var(--red)' : (!hasSpool ? 'var(--orange)' : 'var(--green)');
          const gTextColor = !hasGcode ? '#fff' : (!hasSpool ? '#000' : '#000');
          const gTitle = !hasGcode ? 'Kein G-Code' : (!hasSpool ? 'G-Code OK – Spule fehlt' : 'Komplett');
          const gcodeIcon  = `<span title="${gTitle}" style="position:absolute;top:2px;right:2px;font-size:0.6rem;background:${gColor};color:${gTextColor};border-radius:3px;padding:0 3px;font-weight:700">G</span>`;
          const thumb      = p.thumbnail_b64
            ? `<img src="data:image/png;base64,${p.thumbnail_b64}" style="width:100%;height:52px;object-fit:cover;border-radius:4px 4px 0 0">`
            : `<div style="width:100%;height:52px;background:var(--hover);border-radius:4px 4px 0 0;display:flex;align-items:center;justify-content:center;font-size:1.2rem">🖨️</div>`;
          const info = hasGcode
            ? `<div style="font-size:0.6rem;color:${hasPrice ? 'var(--green)' : 'var(--accent)'}">${p.slicer_time_h}h|${p.slicer_weight_g}g</div>`
            : '<div style="font-size:0.6rem;color:var(--red)">kein G-Code</div>';
          return `<div onclick="openPlateModal(${p.id},${job.id})" title="${gTitle} – klicken"
            style="position:relative;width:72px;cursor:pointer;border:2px solid ${statusColor};border-radius:6px;overflow:hidden;background:var(--card)">
            ${thumb}
            ${gcodeIcon}
            <div style="padding:2px 4px">
              <div style="font-size:0.65rem;font-weight:600;white-space:nowrap;overflow:hidden">P${p.plate_number}</div>
              ${info}
            </div>
          </div>`;
        }).join('')}
      </div>`
    : `<div style="margin-top:6px;font-size:0.75rem;color:var(--muted)">Keine Platten &mdash; .3mf hochladen</div>`;

  // Gesamtzeit + Gewicht aus Platten
  const totalTime = job.plates.reduce((s, p) => s + (p.slicer_time_h || 0), 0);
  const totalWeight = job.plates.reduce((s, p) => s + (p.slicer_weight_g || 0), 0);
  const slicerInfo = totalTime > 0
    ? `<span style="font-size:0.75rem;color:var(--muted)">${totalTime.toFixed(1)}h | ${totalWeight.toFixed(0)}g</span>`
    : '';

  const platesWithPrice = job.plates.filter(p => p.offer_selling_price > 0).length;
  const allPriced = job.plates_total > 0 && platesWithPrice === job.plates_total;
  const somePriced = platesWithPrice > 0 && !allPriced;
  const priceColor = allPriced ? 'var(--green)' : somePriced ? 'var(--orange)' : 'var(--muted)';
  const priceLabel = job.offer_price > 0
    ? `${job.offer_price.toFixed(2)} &#8364;${somePriced ? ' <span style="font-size:0.7rem">('+platesWithPrice+'/'+job.plates_total+')</span>' : ''}`
    : 'kein Angebot';
  const priceTag = `<span style="color:${priceColor};font-weight:600">${priceLabel}</span>`;

  return `<div style="display:flex;align-items:flex-start;gap:1rem;padding:0.75rem;background:var(--hover);border-radius:8px;margin-bottom:0.5rem;border-left:3px solid ${jobStatusColor(job.status)}">
    <div style="flex:1;min-width:0">
      <div style="font-weight:600;font-size:0.95rem">${job.name}</div>
      ${job.description ? `<div style="font-size:0.8rem;color:var(--muted)">${job.description}</div>` : ''}
      <div style="display:flex;gap:0.75rem;align-items:center;margin-top:4px;flex-wrap:wrap">
        <span style="font-size:0.75rem;padding:0.15rem 0.5rem;border-radius:4px;background:${jobStatusColor(job.status)};color:white">${job.status}</span>
        ${job.plates_total > 0 ? `<span style="font-size:0.75rem;color:var(--muted)">${job.plates_done}/${job.plates_total} Platten</span>` : ''}
        ${slicerInfo}
        <span style="font-size:0.75rem;color:var(--muted)">${job.created_at.slice(0,10)}</span>
      </div>
      ${plateBar}
    </div>
    <div style="text-align:right;flex-shrink:0">
      <div style="margin-bottom:0.5rem">${priceTag}</div>
      <div style="display:flex;gap:0.5rem">
        <select onchange="changeJobStatus(${job.id}, this.value)" style="padding:0.3rem;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--fg);font-size:0.75rem">
          ${['Angebot','Warteschlange','In Druck','Abgeschlossen','Abgelehnt','Zurueckgestellt'].map(s =>
            `<option value="${s}"${s === job.status ? ' selected' : ''}>${s}</option>`
          ).join('')}
        </select>
        ${job.offer_price > 0 ? `<a href="/api/jobs/${job.id}/pdf" target="_blank" class="btn-icon" title="${job.status === 'Abgeschlossen' ? 'Rechnung als PDF' : 'Angebot als PDF'}" style="text-decoration:none;font-size:0.9rem">${job.status === 'Abgeschlossen' ? '🧾' : '📄'}</a>` : ''}
        <button class="btn-icon" onclick="openEditJobModal(${job.id})" title="Auftrag bearbeiten">&#9998;</button>
        <button class="btn-icon del" onclick="deleteJob(${job.id})" title="Loeschen">&#128465;</button>
      </div>
    </div>
  </div>`;
}

async function openEditJobModal(jobId) {
  const job = jobsData.find(j => j.id === jobId);
  if (!job) return;
  document.getElementById('edit-job-id').value = jobId;
  document.getElementById('edit-job-name').value = job.name || '';
  document.getElementById('edit-job-desc').value = job.description || '';
  document.getElementById('edit-job-customer-name').value   = job.customer_name   || '';
  document.getElementById('edit-job-customer-street').value = job.customer_street || '';
  document.getElementById('edit-job-customer-city').value   = job.customer_city   || '';
  document.getElementById('edit-job-customer-email').value  = job.customer_email  || '';
  document.getElementById('edit-job-customer-phone').value  = job.customer_phone  || '';
  document.getElementById('edit-job-overlay').classList.add('open');
}

function closeEditJobModal() {
  document.getElementById('edit-job-overlay').classList.remove('open');
}

async function saveEditJob() {
  const jobId = parseInt(document.getElementById('edit-job-id').value);
  const name  = document.getElementById('edit-job-name').value.trim();
  if (!name) { toast('Auftragsname darf nicht leer sein', 'err'); return; }
  const job = jobsData.find(j => j.id === jobId) || {};
  const r = await fetch('/api/jobs/' + jobId, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      name,
      description:     document.getElementById('edit-job-desc').value,
      status:          job.status || 'Angebot',
      profit_margin:   job.profit_margin || 30,
      notes:           job.notes || '',
      customer_name:   document.getElementById('edit-job-customer-name').value.trim(),
      customer_street: document.getElementById('edit-job-customer-street').value.trim(),
      customer_city:   document.getElementById('edit-job-customer-city').value.trim(),
      customer_email:  document.getElementById('edit-job-customer-email').value.trim(),
      customer_phone:  document.getElementById('edit-job-customer-phone').value.trim(),
    })
  });
  const d = await r.json();
  if (d.status === 'ok') {
    const _en = document.getElementById('edit-job-customer-name').value.trim();
    if (_en) upsertCustomer(_en,
      document.getElementById('edit-job-customer-street').value.trim(),
      document.getElementById('edit-job-customer-city').value.trim(),
      document.getElementById('edit-job-customer-email').value.trim(),
      document.getElementById('edit-job-customer-phone').value.trim());
    closeEditJobModal();
    loadJobs();
    toast('Auftrag gespeichert!', 'ok');
  } else {
    toast('Fehler beim Speichern', 'err');
  }
}

async function changeJobStatus(id, status) {
  const job = jobsData.find(j => j.id === id);
  if (!job) return;
  await fetch(`/api/jobs/${id}`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({...job, status})
  });
  loadJobs();
  toast(`Status: ${status}`, 'ok');
}

async function deleteJob(id) {
  if (!confirm('Auftrag wirklich loeschen? Alle Platten werden ebenfalls geloescht.')) return;
  await fetch(`/api/jobs/${id}`, {method: 'DELETE'});
  loadJobs();
  toast('Auftrag geloescht', 'ok');
}

function toggleArchive() {
  archiveVisible = !archiveVisible;
  document.getElementById('jobs-archive').style.display = archiveVisible ? 'block' : 'none';
}

function openJobDetail(id) {
  const job = jobsData.find(j => j.id === id);
  if (!job) return;
  // Einfache Detail-Ansicht als Alert -- wird spaeter durch Modal ersetzt
  alert('Auftrag #' + job.id + ': ' + job.name + '\nStatus: ' + job.status + '\nPlatten: ' + job.plates_total + '\nAngebotspreis: ' + job.offer_price.toFixed(2) + ' EUR\nErstellt: ' + job.created_at);
}

// Neuer Auftrag Modal
function openNewJobModal() {
  document.getElementById('new-job-overlay').classList.add('open');
  document.getElementById('new-job-name').value = '';
  document.getElementById('new-job-desc').value = '';
  document.getElementById('new-job-3mf-result').style.display = 'none';
  document.getElementById('new-job-3mf-result').innerHTML = '';
  document.getElementById('new-job-time').value = '0';
  document.getElementById('new-job-weight').value = '0';
  document.getElementById('new-job-thumbnail').value = '';
  document.getElementById('new-job-3mf-input').value = '';
  document.getElementById('new-job-gcode-input').value = '';
  window._3mfData = null;
  window._gcodeData = null;
}

function closeNewJobModal() {
  document.getElementById('new-job-overlay').classList.remove('open');
}

async function parse3mfFile(input) {
  const file = input.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append('file', file);
  // Kurz prüfen ob es eine ZIP-Datei ist (Magic Bytes PK) bevor Upload
  try {
    const slice = file.slice(0, 2);
    const buf = await slice.arrayBuffer();
    const bytes = new Uint8Array(buf);
    if (bytes[0] !== 0x50 || bytes[1] !== 0x4B) {
      toast('Falsche Datei — bitte "Exportieren aller geslicten Druckplatten" verwenden (.3mf)', 'err');
      input.value = '';
      return;
    }
    toast('Lese .3mf...', 'ok');
    const r = await fetch('/api/jobs/parse-3mf', {method: 'POST', body: formData});
    const data = await r.json();
    if (data.error) { toast(data.error, 'err'); return; }

    const res = document.getElementById('new-job-3mf-result');
    let html = '<div style="background:rgba(34,197,94,0.1);border:1px solid var(--green);border-radius:8px;padding:0.75rem;margin-top:0.75rem">';
    html += '<div style="font-weight:600;color:var(--green);margin-bottom:0.5rem">&#9989; .3mf gelesen</div>';
    html += '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0.5rem;font-size:0.875rem;margin-bottom:0.5rem">';
    html += '<div><span style="color:var(--muted)">Platten:</span> <strong>' + data.plate_count + '</strong></div>';
    if (data.has_slice_data) {
      html += '<div><span style="color:var(--muted)">Gesamtzeit:</span> <strong>' + data.print_time_h + 'h</strong></div>';
      html += '<div><span style="color:var(--muted)">Gewicht:</span> <strong>' + data.total_weight_g + 'g</strong></div>';
    } else {
      html += '<div><span style="color:var(--muted)">Typen:</span> <strong>' + (data.filament_types.join(', ') || '?') + '</strong></div>';
      html += '<div></div>';
    }
    html += '</div>';
    if (data.plates && data.plates.length) {
      html += '<div style="display:flex;gap:0.4rem;flex-wrap:wrap">';
      data.plates.forEach(function(p) {
        html += '<div style="background:var(--hover);border-radius:6px;padding:0.4rem;text-align:center;min-width:75px">';
        if (p.thumbnail_b64) {
          html += '<img src="data:image/png;base64,' + p.thumbnail_b64 + '" style="height:50px;border-radius:3px;display:block;margin:0 auto">';
        }
        html += '<div style="font-size:0.7rem;color:var(--fg);margin-top:2px">' + p.name + '</div>';
        if (p.print_time_h > 0 || p.weight_g > 0) {
          html += '<div style="font-size:0.65rem;color:var(--accent)">' + p.print_time_h + 'h | ' + p.weight_g + 'g</div>';
        }
        html += '</div>';
      });
      html += '</div>';
    }
    if (!data.has_slice_data) {
      html += '<div style="font-size:0.8rem;color:var(--orange);margin-top:0.5rem">&#9888; Kein Slice-Export &mdash; bitte "Exportieren aller geslicten Druckplatten" verwenden</div>';
    }
    html += '</div>';
    res.innerHTML = html;
    res.style.display = 'block';
    window._3mfData = data;
    if (data.thumbnail_b64) document.getElementById('new-job-thumbnail').value = data.thumbnail_b64;
    if (data.has_slice_data) {
      document.getElementById('new-job-time').value = data.print_time_h;
      document.getElementById('new-job-weight').value = data.total_weight_g;
    }
    toast('.3mf gelesen!', 'ok');
  } catch(e) { toast('Fehler: ' + e.message, 'err'); console.error(e); }
}

async function parseGcodeFiles(input) {
  const files = Array.from(input.files);
  if (!files.length) return;

  // Mehrere Dateien: jede separat parsen
  const results = [];
  for (const file of files) {
    const formData = new FormData();
    formData.append('file', file);
    try {
      const r = await fetch('/api/jobs/parse-gcode', {method: 'POST', body: formData});
      const data = await r.json();
      if (!data.error) {
        results.push({filename: file.name, ...data});
      }
    } catch(e) {}
  }

  if (!results.length) { toast('Keine G-Code Daten gelesen', 'err'); return; }

  // Ergebnisse anzeigen
  const res = document.getElementById('new-job-3mf-result');
  // Vorherigen gcode-Info entfernen
  const old = document.getElementById('gcode-parsed-info');
  if (old) old.remove();

  let html = '<div id="gcode-parsed-info" style="background:rgba(59,130,246,0.1);border:1px solid var(--accent);border-radius:8px;padding:0.75rem;margin-top:0.5rem">';
  html += '<div style="font-weight:600;color:var(--accent);margin-bottom:0.5rem">&#9989; ' + results.length + ' G-Code(s) gelesen</div>';

  let totalTime = 0, totalWeight = 0;
  results.forEach(function(d, i) {
    totalTime   += d.print_time_h || 0;
    totalWeight += d.total_weight_g || 0;
    html += '<div style="font-size:0.8rem;padding:0.3rem 0;border-bottom:1px solid var(--border)">';
    html += '<span style="color:var(--muted)">Platte ' + (i+1) + ':</span> ';
    html += '<strong>' + d.print_time_h + 'h</strong> | <strong>' + d.total_weight_g + 'g</strong>';
    html += ' <span style="color:var(--muted);font-size:0.75rem">' + d.filename.replace(/\.gcode$/i,'') + '</span>';
    html += '</div>';
  });
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem;margin-top:0.5rem;font-size:0.875rem">';
  html += '<div><span style="color:var(--muted)">Gesamt:</span> <strong>' + totalTime.toFixed(2) + 'h</strong></div>';
  html += '<div><span style="color:var(--muted)">Gesamt:</span> <strong>' + totalWeight.toFixed(1) + 'g</strong></div>';
  html += '</div></div>';

  res.innerHTML = (res.innerHTML || '') + html;
  res.style.display = 'block';

  // Wenn eine .3mf dabei war, deren Platten direkt nutzen
  const threemf = results.find(function(r) { return r.is_3mf; });
  if (threemf && threemf.plates && threemf.plates.length > 1) {
    window._gcodeData = {
      print_time_h:   threemf.print_time_h,
      total_weight_g: threemf.total_weight_g,
      plates:         threemf.plates.map(function(p) {
        return {print_time_h: p.print_time_h, total_weight_g: p.weight_g, filaments: p.filaments || []};
      })
    };
  } else {
    window._gcodeData = {
      print_time_h:   totalTime,
      total_weight_g: totalWeight,
      plates:         results
    };
  }
  document.getElementById('new-job-time').value   = totalTime;
  document.getElementById('new-job-weight').value = totalWeight;
  toast(results.length + ' G-Code(s) gelesen!', 'ok');
}

// G-Code Daten aus Plattendetail zurücksetzen
function clearPlateGcode() {
  document.getElementById('plate-slicer-weight').value = 0;
  document.getElementById('plate-slicer-time').value = 0;
  plateFilaments = [];
  renderPlateFilaments();
  const gcNameEl = document.getElementById('plate-gcode-name');
  const gcClearEl = document.getElementById('plate-gcode-clear');
  const gcInput  = document.getElementById('plate-gcode-input');
  if (gcNameEl)  { gcNameEl.innerHTML = ''; gcNameEl.style.display = 'none'; }
  if (gcClearEl) { gcClearEl.style.display = 'none'; }
  if (gcInput)   { gcInput.value = ''; }
  toast('G-Code Daten zurückgesetzt', 'ok');
}

async function loadGcodeIntoPlate(input) {
  if (!file) return;
  const formData = new FormData();
  formData.append('file', file);
  try {
    toast('Lese G-Code...', 'ok');
    const r = await fetch('/api/jobs/parse-gcode', {method: 'POST', body: formData});
    const data = await r.json();
    if (data.error) { toast(data.error, 'err'); return; }

    // Werte in Plattendetail-Felder eintragen
    if (data.print_time_h > 0)
      document.getElementById('plate-slicer-time').value = data.print_time_h;
    if (data.total_weight_g > 0)
      document.getElementById('plate-slicer-weight').value = data.total_weight_g;

    // Filamente befüllen falls vorhanden
    if (data.filaments && data.filaments.length > 0) {
      plateFilaments = data.filaments
        .filter(function(f) { return f.weight_g > 0; })
        .map(function(f) { return {uid: '', weight_g: f.weight_g, material: f.material || ''}; });
      renderPlateFilaments();
    }

    toast(data.print_time_h + 'h | ' + data.total_weight_g + 'g aus G-Code geladen', 'ok');

    // Dateiname als Bestätigung im Modal anzeigen
    const gcNameEl = document.getElementById('plate-gcode-name');
    const gcClearEl = document.getElementById('plate-gcode-clear');
    if (gcNameEl) {
      gcNameEl.innerHTML = '&#10003; ' + file.name;
      gcNameEl.style.display = 'inline';
    }
    if (gcClearEl) gcClearEl.style.display = 'inline';
    // Input zurücksetzen
    input.value = '';
  } catch(e) { toast('Fehler beim Lesen des G-Codes', 'err'); }
}

// ===== KUNDENDATENBANK v2.13 =====
let _customers = [];

async function loadCustomers() {
  try {
    const r = await fetch('/api/customers');
    _customers = await r.json();
    const dl = document.getElementById('customer-name-list');
    if (dl) {
      dl.innerHTML = _customers.map(c => `<option value="${c.name}">`).join('');
    }
  } catch(e) { console.warn('loadCustomers:', e); }
}

function onCustomerNameInput() {
  const val = document.getElementById('edit-job-customer-name').value.trim();
  const match = _customers.find(c => c.name.toLowerCase() === val.toLowerCase());
  if (match) {
    document.getElementById('edit-job-customer-street').value = match.street || '';
    document.getElementById('edit-job-customer-city').value   = match.city   || '';
    document.getElementById('edit-job-customer-email').value  = match.email  || '';
    document.getElementById('edit-job-customer-phone').value  = match.phone  || '';
  }
}

async function upsertCustomer(name, street, city, email, phone) {
  if (!name) return;
  try {
    await fetch('/api/customers/upsert', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, street, city, email, phone})
    });
    await loadCustomers();
  } catch(e) { console.warn('upsertCustomer:', e); }
}

async function saveNewJob() {
  const name = document.getElementById('new-job-name').value.trim();
  if (!name) { toast('Name erforderlich', 'err'); return; }

  // Auftrag anlegen
  const jobResp = await fetch('/api/jobs', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      name,
      description: document.getElementById('new-job-desc').value,
      status: 'Angebot',
    })
  });
  const jobData = await jobResp.json();
  const jobId = jobData.id;

  // Platten anlegen — nur aus .3mf Slice-Daten
  const d3mf = window._3mfData;

  if (d3mf && d3mf.has_slice_data && d3mf.plates && d3mf.plates.length > 0) {
    // .3mf mit Slice-Daten: eine Platte pro Platte
    for (const plate of d3mf.plates) {
      await fetch('/api/jobs/' + jobId + '/plates', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          slicer_time_h:   plate.print_time_h,
          slicer_weight_g: plate.weight_g,
          thumbnail_b64:   plate.thumbnail_b64 || null,
          filaments: plate.filaments.map(function(f) {
            return {uid: '', weight_g: f.weight_g, material: f.material || '', color: f.color || ''};
          })
        })
      });
    }
  }
  // Kein .3mf → keine Platten, können später im Plattendetail per G-Code geladen werden

  // Thumbnail speichern falls vorhanden
  const thumb = document.getElementById('new-job-thumbnail').value;
  if (thumb) {
    await fetch('/api/jobs/' + jobId, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        name,
        description:     document.getElementById('new-job-desc').value,
        status:          'Angebot',
        profit_margin:   30,
        notes:           '',
      })
    });
  }

  // State zurücksetzen
  window._3mfData = null;
  window._gcodeData = null;

  closeNewJobModal();
  await loadJobs();  // warten bis Jobs geladen

  toast('Auftrag "' + name + '" angelegt! Platte anklicken um Kalkulation zu starten.', 'ok');
  toast(null); // vorherigen toast löschen lassen

  toast('Auftrag "' + name + '" angelegt!', 'ok');
}


</script>
</body>
</html>"""



if __name__ == "__main__":
    init_db()
    log.info(f"FilaStation Server v2.13 startet auf {HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False)
