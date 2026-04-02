#!/usr/bin/env python3
"""
FilaStation v2.12 - OLED Display + Server Command Polling + Creality CFS NFC (Encrypted, UART)
Hardware:  Pi3 + PN532 (UART /dev/ttyS0) + HX711 (GPIO23/24) + SSD1306 OLED (Software-I2C GPIO17/27)
Server:    Pi4 @ 192.168.178.31:5000

NEU in v2.12 (gegenueber v2.11) – HX711 Library Fix:
- BUG FIX (kritisch): Waage zeigte immer 0.0g
    URSACHE: Code nutzte get_raw_data() von einer anderen HX711-Library
    INSTALLIERT ist tatobari/hx711 mit API: readRawBytes(), read_average(), get_weight(), tare()
    JETZT: Waage-Klasse komplett auf tatobari/hx711 API umgestellt
    - raw() nutzt jetzt readRawBytes() direkt
    - grams() nutzt read_average(5) fuer stabile Messung
    - tare() nutzt die eingebaute tare()-Funktion der Library
    - calibrate_500g() passt reference_unit an (Library-nativer Weg)
    - Kalibrierung: set_reference_unit(1) + tare() + 500g messen → neuer reference_unit

NEU in v2.11 (gegenueber v2.10) – Creality CFS Kompatibilitaets-Fixes:
- BUG FIX (kritisch): Sector-Trailer korrigiert:
    VORHER: KeyA=000000 | AccessBits | KeyB=SectorKey  ← CFS konnte Tag nicht lesen!
    JETZT:  KeyA=SectorKey | AccessBits | KeyB=SectorKey  ← wie echte Creality-Tags
- BUG FIX: Re-Write bereits beschriebener Tags setzt jetzt ebenfalls den Trailer neu
    (Authorizierung ueber KeyB=SectorKey, dann Trailer mit neuen Keys ueberschreiben)
- BUG FIX: _build_plaintext nutzt jetzt echtes Datum via _encode_date() statt
    festem Prefix "AB124" (war inkonsistent mit der vorhandenen _encode_date()-Funktion)
- Sector 2 Re-Write: Auth-Fallback auf SectorKey wenn Factory-Key versagt
- Verbesserte Log-Ausgaben fuer einfacheres Debugging

NEU in v2.10 (bleibt erhalten):
- Verschluesselung: AES/ECB statt AES/CBC (Quelle: CFTag Utils.java)
- Padding: Space-Padding auf 96 Zeichen
- Sector 2 (Blocks 8,9,10) unverschluesselt
- _derive_sector_key: uid raw bytes * 4

NEU in v2.9 (gegenueber v2.8):
- AES Keys korrekt reverse-engineered aus proxmark4cfs.html (JavaScript-Quelle)
- SECTOR_KEY_SECRET: Creality-Firmware-Key für Sector-Key-Ableitung (im Code definiert)
- AES_MASTER_KEY:    Creality-Firmware-Key für Daten-Verschlüsselung (im Code definiert)
- _derive_sector_key(): UID als Hex-String 4x wiederholen → AES-ECB
- Verifiziert gegen 3 echte Creality-Tags (90D90912, B08C8D39, 90E00912)

NEU in v2.8 (gegenüber v2.7):
- PN532 Kommunikation auf UART umgestellt (/dev/ttyS0, 115200 Baud)
- Stabiler als I2C (kein Freeze-Problem)
- DIP-Schalter: SW1=L, SW2=L (UART-Modus)
- Verdrahtung: PN532-TX → Pi-GPIO15(RXD), PN532-RX → Pi-GPIO14(TXD)

NEU in v2.7 (bleibt erhalten):
- AES-128-CBC Verschlüsselung des Tag-Inhalts (Pflicht ab CFS-Firmware ~Jan 2025)
- UID-basierte Sector-1 Key-Ableitung (AES-Verschlüsselung von UID mit hardcoded Master-Key)
- Sector-1 wird mit dem abgeleiteten Key geschützt (statt FFFFFFFFFFFF)
- Nur 3 Blöcke werden beschrieben: Block 4, 5, 6 (Sektor 1 Daten) + Sector-Trailer Block 7
- pycryptodome wird benötigt: pip install pycryptodome --break-system-packages

Abhängigkeiten installieren:
  pip install pycryptodome --break-system-packages
  pip install pyserial --break-system-packages

NEU in v2.6 (bleibt erhalten):
- Creality CFS-kompatibles NFC Tag-Format (MIFARE Classic 1K)
  → 20-Byte Nutzlast mit Datum, VendorID, Material-ID, RGB-Farbe, Gewicht, Seriennummer
  → Unterstützt direkte RGB-Hex Farben (#RRGGBB) sowie deutsche Farbnamen
  → Gewichts-Buckets: 250g / 500g / 600g / 750g / 1000g
- write_block1() bleibt als Alias für Rückwärtskompatibilität

Alle v2.5 Features bleiben erhalten:
- Server Command Polling (alle 2 Sekunden)
- Remote TARE, Kalibrierung, Reboot, Shutdown über Webinterface
"""

import time, os, json, logging, sys, requests, serial
from pathlib import Path
from hx711 import HX711

# Waveshare PN532 Library (zuverlaessiger als Adafruit auf diesem HAT)
sys.path.insert(0, str(Path.home()))  # FIX: /home/<user>/ damit pn532/__init__.py gefunden wird
from pn532 import PN532_UART as WavesharePN532

# UART-Port für PN532 (Waveshare HAT nutzt ttyS0)
PN532_UART_PORT = "/dev/ttyS0"
PN532_UART_BAUD = 115200

# AES für Creality CFS Tag-Verschlüsselung
try:
    from Crypto.Cipher import AES
    AES_AVAILABLE = True
except ImportError:
    AES_AVAILABLE = False
    print("⚠️  pycryptodome nicht installiert!")
    print("   sudo pip install pycryptodome --break-system-packages")

# OLED mit luma.oled über Hardware-I2C (Bus 1, GPIO2=SDA, GPIO3=SCL)
OLED_AVAILABLE = False
try:
    from luma.core.interface.serial import i2c as luma_i2c
    from luma.oled.device import ssd1306
    from luma.core.render import canvas as luma_canvas
    from PIL import Image, ImageDraw, ImageFont
    OLED_AVAILABLE = True
except ImportError:
    print("⚠️  luma.oled nicht installiert!")
    print("   pip install luma.oled --break-system-packages")

# IP des Pi 4 (FilaStation-Server) — wird aus waage_config.json geladen (Feld "server_url")
# Hier nur der Fallback-Wert, falls die Config keinen Eintrag hat.
# Bitte in waage_config.json anpassen: "server_url": "http://<PI4-IP>:5000"
SERVER_URL  = "http://192.168.1.100:5000"
CONFIG_FILE = Path.home() / "waage_config.json"
LOG_FILE    = Path.home() / "waage.log"
HX_DOUT, HX_SCK = 23, 24
OLED_I2C_ADDRESS = 0x3C
OLED_I2C_PORT    = 1       # Hardware I2C Bus 1 (GPIO2=SDA, GPIO3=SCL)

DEFAULT_CONFIG = {
    "server_url":  "http://192.168.1.100:5000",   # IP des Pi 4, bitte anpassen!
    "master_tags": {"tare": None, "calibrate": None, "shutdown": None, "reboot": None},
    "calibration": {"gram_factor": 416.9286, "zero_baseline": -23061.7},
    "registered":  False,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Config-Fehler: {e}")
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


class OLEDDisplay:
    """OLED Display via luma.oled + Hardware-I2C (Bus 1, GPIO2=SDA, GPIO3=SCL)."""

    def __init__(self):
        self.device = None
        self.image  = None
        self.draw   = None
        self.font_normal = None
        self.font_small  = None

        if not OLED_AVAILABLE:
            log.warning("⚠️  OLED nicht verfügbar (luma.oled fehlt)")
            return

        try:
            serial = luma_i2c(port=OLED_I2C_PORT, address=OLED_I2C_ADDRESS)
            self.device = ssd1306(serial)

            self.image = Image.new('1', (128, 64))
            self.draw  = ImageDraw.Draw(self.image)

            try:
                self.font_normal = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 14)
                self.font_small  = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 10)
            except:
                self.font_normal = ImageFont.load_default()
                self.font_small  = ImageFont.load_default()

            self.clear()
            log.info("✅ OLED Display bereit (luma.oled Hardware-I2C Bus 1, 0x3C)")

        except Exception as e:
            log.error(f"❌ OLED Init Fehler: {e}")
            self.device = None

    def _update_display(self):
        """PIL-Image direkt an luma.oled übergeben."""
        if not self.device:
            return
        self.device.display(self.image)

    def clear(self):
        if self.device:
            self.draw.rectangle((0, 0, 127, 63), outline=0, fill=0)
            self._update_display()

    def show_boot(self, step="Starte..."):
        if not self.device:
            return
        self.draw.rectangle((0, 0, 127, 63), outline=0, fill=0)
        self.draw.rectangle((0, 0, 127, 12), outline=1, fill=1)
        self.draw.text((2, 1), "FILASTATION", fill=0, font=self.font_small)
        self.draw.text((10, 25), "STARTE...", fill=1, font=self.font_normal)
        self.draw.text((5, 50), step, fill=1, font=self.font_small)
        self._update_display()

    def show_main(self, weight=None, status="BEREIT", server_ok=True):
        if not self.device:
            return
        
        self.draw.rectangle((0, 0, 127, 63), outline=0, fill=0)
        self.draw.rectangle((0, 0, 127, 12), outline=1, fill=1)
        
        srv_icon = "🟢" if server_ok else "🔴"
        self.draw.text((2, 1), f"FILASTATION", fill=0, font=self.font_small)
        
        if weight is not None:
            w_text = f"{weight:.0f}g"
            self.draw.text((40, 25), w_text, fill=1, font=self.font_normal)
        
        self.draw.text((10, 50), status, fill=1, font=self.font_small)
        self._update_display()
    
    def update_weight(self, weight):
        if not self.device:
            return
        
        # Nur Gewicht aktualisieren ohne ganzes Display neu zu zeichnen
        self.draw.rectangle((30, 20, 100, 40), outline=0, fill=0)
        w_text = f"{weight:.0f}g"
        self.draw.text((40, 25), w_text, fill=1, font=self.font_normal)
        self._update_display()
    
    def show_spool(self, uid, material, color, weight, empty_w, full_w):
        if not self.device:
            return
        
        self.draw.rectangle((0, 0, 127, 63), outline=0, fill=0)
        self.draw.rectangle((0, 0, 127, 12), outline=1, fill=1)
        self.draw.text((2, 1), "SPULE ERKANNT", fill=0, font=self.font_small)
        
        self.draw.text((25, 18), f"{weight:.0f}g", fill=1, font=self.font_normal)
        self.draw.text((5, 35), f"{material} {color[:6]}", fill=1, font=self.font_small)
        
        # Fortschrittsbalken
        if full_w > empty_w:
            pct = min(100, max(0, (weight - empty_w) / (full_w - empty_w) * 100))
            bar_w = int(pct / 100 * 100)
            self.draw.rectangle((14, 50, 114, 58), outline=1, fill=0)
            if bar_w > 0:
                self.draw.rectangle((15, 51, 14 + bar_w, 57), outline=1, fill=1)
            self.draw.text((118, 50), f"{pct:.0f}%", fill=1, font=self.font_small)
        
        self._update_display()
    
    def show_new_spool(self, uid):
        if not self.device:
            return
        
        self.draw.rectangle((0, 0, 127, 63), outline=0, fill=0)
        self.draw.rectangle((0, 0, 127, 12), outline=1, fill=1)
        self.draw.text((2, 1), "NEUE SPULE!", fill=0, font=self.font_small)
        
        self.draw.text((5, 20), "UID:", fill=1, font=self.font_small)
        self.draw.text((5, 32), uid[:12], fill=1, font=self.font_normal)
        self.draw.text((5, 50), "Im Web konfigurieren", fill=1, font=self.font_small)
        self._update_display()
    
    def show_msg(self, title, msg, duration=2):
        if not self.device:
            return
        
        self.draw.rectangle((0, 0, 127, 63), outline=0, fill=0)
        self.draw.rectangle((0, 0, 127, 12), outline=1, fill=1)
        self.draw.text((2, 1), title[:18], fill=0, font=self.font_small)
        
        self.draw.text((10, 30), msg[:20], fill=1, font=self.font_normal)
        self._update_display()
        
        if duration > 0:
            time.sleep(duration)
    
    def show_register(self, name, step, total):
        if not self.device:
            return
        
        self.draw.rectangle((0, 0, 127, 63), outline=0, fill=0)
        self.draw.rectangle((0, 0, 127, 12), outline=1, fill=1)
        self.draw.text((2, 1), "MASTER SETUP", fill=0, font=self.font_small)
        
        self.draw.text((5, 20), f"Schritt {step}/{total}", fill=1, font=self.font_normal)
        self.draw.text((5, 40), name, fill=1, font=self.font_small)
        self.draw.text((5, 52), "Tag auflegen...", fill=1, font=self.font_small)
        self._update_display()
    
    def show_calibration(self, step, hint=""):
        if not self.device:
            return
        
        self.draw.rectangle((0, 0, 127, 63), outline=0, fill=0)
        self.draw.rectangle((0, 0, 127, 12), outline=1, fill=1)
        self.draw.text((2, 1), "KALIBRIERUNG", fill=0, font=self.font_small)
        
        self.draw.text((5, 20), step, fill=1, font=self.font_normal)
        if hint:
            self.draw.text((5, 45), hint, fill=1, font=self.font_small)
        self._update_display()
    
    def show_nfc_write(self, uid):
        if not self.device:
            return
        
        self.draw.rectangle((0, 0, 127, 63), outline=0, fill=0)
        self.draw.text((5, 15), "NFC UPDATE", fill=1, font=self.font_normal)
        self.draw.text((5, 35), "Tag beschrieben!", fill=1, font=self.font_small)
        self.draw.text((5, 50), uid[:12], fill=1, font=self.font_small)
        self._update_display()
        time.sleep(1.5)
    
    def close(self):
        if self.device:
            self.clear()
            self.device.cleanup()
            log.info("❌ OLED Display geschlossen")


class Waage:
    """
    HX711-Waage mit tatobari/hx711 Library.
    HINWEIS: Diese Waage ist invertiert montiert – Rohwert sinkt wenn Gewicht aufgelegt wird.
    Daher: grams = (baseline - raw) / factor  (statt raw - baseline)
    """
    def __init__(self, cfg):
        self.hx = HX711(HX_DOUT, HX_SCK)
        self.hx.set_reading_format("MSB", "MSB")
        self.factor   = cfg["calibration"]["gram_factor"]
        self.baseline = cfg["calibration"]["zero_baseline"]
        self.hx.reset()
        import time as _t; _t.sleep(0.5)
        log.info(f"✅ HX711 init (tatobari, invertiert): Faktor={self.factor}, Baseline={self.baseline}")

    def raw(self):
        """Einen gemittelten Rohwert lesen."""
        try:
            total = 0
            count = 0
            for _ in range(5):
                b = self.hx.readRawBytes()
                if b:
                    total += (b[0] << 16) | (b[1] << 8) | b[2]
                    count += 1
            if count > 0:
                return total / count
        except Exception as e:
            log.warning(f"HX711 raw() Fehler: {e}")
        return None

    def grams(self):
        """Gewicht in Gramm – invertiert: baseline - raw."""
        r = self.raw()
        if r is None:
            return 0.0
        # Invertiert: Wert sinkt wenn Gewicht drauf kommt
        return round(max(0.0, (self.baseline - r) / self.factor), 1)

    def tare(self):
        """Nullstellung – Baseline auf aktuellen Rohwert setzen."""
        r = self.raw()
        if r is not None:
            self.baseline = r
            log.info(f"Tare OK: Neue Baseline={self.baseline:.1f}")
            return True
        log.error("Tare fehlgeschlagen – kein Rohwert")
        return False

    def calibrate_500g(self):
        """Kalibrierung mit 500g: berechnet neuen Faktor."""
        r = self.raw()
        if r is None:
            log.error("Kalibrierung: Kein Rohwert")
            return None
        # Invertiert: baseline - raw = 500g
        net = self.baseline - r
        if net <= 0:
            log.error(f"Kalibrierung: Netto-Rohwert <= 0 ({net:.1f}), bitte 500g auflegen!")
            return None
        new_factor = net / 500.0
        self.factor = new_factor
        log.info(f"Kalibrierung OK: Faktor={new_factor:.4f} (baseline={self.baseline:.0f}, raw={r:.0f}, netto={net:.0f})")
        return new_factor


class NFC:
    """
    NFC-Klasse für Creality CFS-kompatible Tags (verschlüsselt, ab Firmware ~Jan 2025).

    Das CFS-Format (reverse engineered, community):
    ─────────────────────────────────────────────────────────────────────────────
    PLAINTEXT-NUTZLAST (20 Byte = 40 Nibbles):
      [Date 5N][VendorID 4N][Batch 2N][FilamentID 6N][Color 7N][Len 4N][Serial 6N][Reserve 6N]

    VERSCHLÜSSELUNG (ab neuerer CFS-Firmware):
      • Nutzlast (20 Byte) wird auf 48 Byte PKCS#7-padded
      • AES-128-CBC mit IV=0x00*16 und einem FESTEN Master-Key (hardcoded in Creality-Firmware)
      • Der MIFARE Sector-1-Key (Key A) wird aus der Tag-UID abgeleitet:
          sector_key = AES-128-ECB(master_key, uid_padded_to_16_bytes)[:6]
      • Die 48 Byte Ciphertext werden auf Blöcke 4, 5, 6 geschrieben (je 16 Byte)
      • Block 7 (Sector Trailer) wird mit dem abgeleiteten Key A gesetzt

    LAYOUT AUF DEM TAG:
      Block 4 : Ciphertext-Bytes  0-15
      Block 5 : Ciphertext-Bytes 16-31
      Block 6 : Ciphertext-Bytes 32-47
      Block 7 : Sector Trailer = [sector_key_a (6B)][access_bits (4B)][sector_key_b (6B)]

    Abhängigkeit: pip install pycryptodome --break-system-packages
    ─────────────────────────────────────────────────────────────────────────────
    """

    # ── Creality CFS AES Keys (reverse engineered aus proxmark4cfs.html JS-Quelle) ──
    # Verifiziert gegen 3 echte UID/SectorKey-Paare am 2026-03-13:
    #   UID 90D90912 → SectorKey 66993B6A2CAC ✅
    #   UID B08C8D39 → SectorKey 825CE0992410 ✅
    #   UID 90E00912 → SectorKey 98F368E022B5 ✅
    #
    # Sector Key Ableitung: AES-ECB(SECTOR_KEY_SECRET, uid*4)[:6]
    # Data Verschlüsselung:  AES-CBC(DATA_KEY, plaintext, IV=0x00*16)
    SECTOR_KEY_SECRET = b"q3bu^t1nqfZ(pf$1"   # 16 bytes, für Sector Key Ableitung
    AES_MASTER_KEY    = b"H@CFkRnz@KAtBJp2"   # 16 bytes, für Daten-Verschlüsselung

    # Sector-Trailer Access-Bits für Sektor 1:
    # FF 07 80 69 = Standard-Transport-Konfiguration (KeyA darf lesen/schreiben)
    SECTOR_TRAILER_ACCESS = bytes.fromhex("FF078069")

    # ── Material-IDs (universell kompatibel, alle CFS-Firmwares) ─────────────
    FILAMENT_IDS = {
        "PLA":        "000001",   # Generic PLA
        "PLA_MATTE":  "000002",   # Generic PLA Silk (Matte ähnlich)
        "PETG":       "000003",   # Generic PETG
        "ABS":        "000004",   # Generic ABS
        "ASA":        "000007",   # Generic ASA
        "TPU":        "000005",   # Generic TPU
        "PA":         "000008",   # Generic PA (Nylon)
        "PC":         "000021",   # Generic PC
        "PVA":        "000011",   # Generic PVA
        "PETG_CF":    "000014",   # Generic PETG-CF
        "PLA_CF":     "000006",   # Generic PLA-CF
        "PLA_SILK":   "000002",   # Generic PLA-Silk
        # Creality-eigene Typen (für Hyper-Filamente)
        "HYPER_PLA":  "010001",   # Hyper PLA
        "HYPER_PETG": "060002",   # Hyper PETG
        "HYPER_ABS":  "030001",   # Hyper ABS
        "HYPER_ASA":  "020001",   # Hyper ASA (=HP-ASA)
        "CR_PLA":     "040001",   # CR-PLA
        "CR_PETG":    "060001",   # CR-PETG
        "CR_ABS":     "070001",   # CR-ABS
    }

    # Farb-Name → RGB-Hex
    COLOR_RGB = {
        "SCHWARZ":  "000000",
        "WEISS":    "FFFFFF",
        "ROT":      "FF0000",
        "BLAU":     "0000FF",
        "GRUEN":    "00FF00",
        "GRAU":     "808080",
        "GELB":     "FFFF00",
        "ORANGE":   "FF6600",
        "VIOLETT":  "8000FF",
        "LILA":     "8000FF",
        "BRAUN":    "8B4513",
        "NATUR":    "F5F0DC",
        "PINK":     "FF69B4",
        "CYAN":     "00FFFF",
        "SILBER":   "C0C0C0",
        "GOLD":     "FFD700",
    }

    # Gewichts-Buckets (Netto-Filamentgewicht → 4 Nibbles hex)
    # 0x0165 = 357 (Dezimal) → entspricht ~500g Netto? 
    # Community: 0330 = 1kg, 0165 = 500g, 00FA = 250g
    WEIGHT_BUCKETS = [
        (250,  "00FA"),
        (500,  "0165"),
        (600,  "01C2"),
        (750,  "02EE"),
        (1000, "0330"),
    ]

    VENDOR_ID   = "0276"   # Fallback VendorID
    VENDOR_NAME = ""       # Hersteller-Name fuer VendorID-Berechnung (leer = VENDOR_ID nutzen)
    BATCH_CODE  = "A2"

    # ─────────────────────────────────────────────────────────────────────────
    def __init__(self):
        if not AES_AVAILABLE:
            log.error("❌ pycryptodome fehlt – AES-Verschlüsselung nicht möglich!")
            log.error("   pip install pycryptodome --break-system-packages")

        max_attempts = 10
        for attempt in range(max_attempts):
            try:
                # Waveshare-Library oeffnet Port selbst (ttyS0, reset=GPIO20)
                self.pn = WavesharePN532(debug=False, reset=20)
                ic, ver, rev, support = self.pn.get_firmware_version()
                self.pn.SAM_configuration()
                # _uart fuer Raw-Frames (bereits von Waveshare geoeffnet)
                self._uart = self.pn._uart
                log.info(f"✅ PN532 NFC bereit via Waveshare-Library ({PN532_UART_PORT}) "
                         f"– Firmware {ver}.{rev}")
                return
            except Exception as e:
                log.warning(f"PN532 Init Versuch {attempt+1}/{max_attempts}: {e}")
                time.sleep(0.5)
        raise RuntimeError("PN532 Init fehlgeschlagen")

    # ── Raw-Frame Kommunikation (umgeht Adafruit-Library fuer Schreiboperationen) ──

    def _pn532_frame(self, data: bytes) -> bytes:
        """Baut einen PN532 UART-Frame: 00 00 FF LEN LCS TFI CMD... DCS 00"""
        tfi_cmd = bytes([0xD4]) + data
        length = len(tfi_cmd)
        lcs = (~length + 1) & 0xFF
        dcs = (~sum(tfi_cmd) + 1) & 0xFF
        return bytes([0x00, 0x00, 0xFF, length, lcs]) + tfi_cmd + bytes([dcs, 0x00])

    def _pn532_send(self, data: bytes, timeout: float = 1.0) -> bytes:
        """Sendet Raw-Frame an PN532 und liest Antwort. Gibt Nutzdaten zurueck (ohne TFI/CMD)."""
        frame = self._pn532_frame(data)
        # Pause + Buffer leeren bevor neuer Befehl gesendet wird.
        # Benoetigt nach read_passive_target damit PN532 bereit ist.
        time.sleep(0.1)
        self._uart.read(self._uart.in_waiting)
        self._uart.write(frame)

        # ACK lesen (6 Byte: 00 00 FF 00 FF 00)
        deadline = time.monotonic() + timeout
        ack = b''
        while len(ack) < 6 and time.monotonic() < deadline:
            chunk = self._uart.read(6 - len(ack))
            if chunk:
                ack += chunk
        if ack != b'\x00\x00\xFF\x00\xFF\x00':
            raise RuntimeError(f"PN532 ACK erwartet, bekommen: {ack.hex()}")

        # Response-Frame lesen
        header = b''
        while len(header) < 5 and time.monotonic() < deadline:
            chunk = self._uart.read(5 - len(header))
            if chunk:
                header += chunk
        if len(header) < 5:
            raise RuntimeError("PN532 Response-Header Timeout")

        length = header[3]
        # Nutzdaten + DCS + 0x00 lesen
        payload = b''
        needed = length + 2
        while len(payload) < needed and time.monotonic() < deadline:
            chunk = self._uart.read(needed - len(payload))
            if chunk:
                payload += chunk

        if len(payload) < needed:
            raise RuntimeError("PN532 Response-Payload Timeout")

        # TFI=D5, CMD=Befehl+1, dann Nutzdaten
        return payload[2:length]  # ohne TFI(D5), CMD, DCS, 00

    def _raw_authenticate(self, uid_bytes: bytes, block: int, key_type: int, key: bytes) -> bool:
        """
        MIFARE-Authentifizierung direkt via InDataExchange (0x40).
        key_type: 0x60=KeyA, 0x61=KeyB
        Umgeht Adafruit-Library komplett.
        """
        # InDataExchange: Tg=01, AuthCmd, Block, Key(6), UID(4)
        params = bytes([0x01, key_type, block]) + key + uid_bytes
        cmd = bytes([0x40]) + params
        try:
            resp = self._pn532_send(cmd, timeout=1.0)
            return len(resp) >= 1 and resp[0] == 0x00
        except Exception as e:
            log.debug(f"_raw_authenticate: {e}")
            return False

    def _raw_write_block(self, block: int, data: bytes) -> bool:
        """MIFARE Block schreiben direkt via InDataExchange (0x40) + MIFARE_WRITE (0xA0)."""
        if len(data) != 16:
            raise ValueError(f"Block-Daten muessen 16 Byte sein, nicht {len(data)}")
        params = bytes([0x01, 0xA0, block]) + data
        cmd = bytes([0x40]) + params
        try:
            resp = self._pn532_send(cmd, timeout=1.0)
            return len(resp) >= 1 and resp[0] == 0x00
        except Exception as e:
            log.debug(f"_raw_write_block {block}: {e}")
            return False

    def read(self):
        """
        Liest einen NFC-Tag via Adafruit-Library (funktioniert zuverlaessig).
        Gibt (uid_str, uid_bytes) zurueck oder (None, None).
        Die uid_bytes bleiben fuer den Schreibvorgang gueltig –
        der Tag ist nach InListPassiveTarget selektiert (Tg=1).
        """
        try:
            uid_bytes = self.pn.read_passive_target(timeout=0.1)
            if uid_bytes:
                uid_str = "".join(f"{b:02X}" for b in uid_bytes)
                return uid_str, uid_bytes
        except Exception as e:
            log.debug(f"NFC read: {e}")
        return None, None

    def read_raw_blocks(self, uid_bytes, sector=1, key=None):
        """
        Liest Sektor roh (Diagnose). Gibt Blöcke als Hex-Strings aus.
        Nützlich zum Vergleich: was schreibt die CFS RFID App vs. unser Code?

        Aufruf im Test:
            uid_bytes = bytes.fromhex("AABBCCDD")
            nfc.read_raw_blocks(uid_bytes)
        """
        if key is None:
            key = b'\xFF\xFF\xFF\xFF\xFF\xFF'
        first_block = sector * 4  # Sektor 1 → Block 4

        if not self.pn.mifare_classic_authenticate_block(uid_bytes, first_block, 0xFF, key):
            log.warning(f"⚠️  read_raw_blocks: Auth mit Standard-Key fehlgeschlagen")
            return None

        result = {}
        for blk in range(first_block, first_block + 4):
            data = self.pn.mifare_classic_read_block(blk)
            if data:
                result[blk] = data.hex().upper()
                log.info(f"   Block {blk}: {data.hex().upper()}")
            else:
                log.warning(f"   Block {blk}: Lesen fehlgeschlagen")
        return result

    # ── Hilfsfunktionen ───────────────────────────────────────────────────────

    def _encode_date(self):
        """5 Nibbles: Nibble0=Monat(hex), Nibble1-2=Tag(BCD), Nibble3-4=Jahr(hex)"""
        t = time.localtime()
        day_bcd = ((t.tm_mday // 10) << 4) | (t.tm_mday % 10)
        return f"{t.tm_mon:01X}{day_bcd:02X}{(t.tm_year % 100):02X}"

    def _resolve_color_hex(self, color):
        c = color.upper().strip().lstrip("#")
        if len(c) == 6:
            try:
                int(c, 16)
                return c
            except ValueError:
                pass
        return self.COLOR_RGB.get(c, "808080")

    def _resolve_material_id(self, material):
        key = material.upper().replace("-", "_").replace(" ", "_")
        return self.FILAMENT_IDS.get(key, "000001")

    def _resolve_weight_bucket(self, full_w, empty_w=220):
        """Wählt den nächsten Gewichts-Bucket. Versucht Netto-Gewicht zu schätzen."""
        net = max(0, full_w - empty_w)
        best_code, best_diff = "0330", float('inf')
        for bucket_g, bucket_hex in self.WEIGHT_BUCKETS:
            if abs(net - bucket_g) < best_diff:
                best_diff = abs(net - bucket_g)
                best_code = bucket_hex
        return best_code

    def _generate_vendor_id(self):
        """
        Generiert 4-Zeichen VendorID aus Java hashCode des Hersteller-Namens.
        Konfigurierbar via VENDOR_NAME. Default "0276" fuer generische Spulen.
        """
        def java_hash(s):
            h = 0
            for c in s:
                h = (31 * h + ord(c)) & 0xFFFFFFFF
            if h >= 0x80000000:
                h -= 0x100000000
            return h
        if self.VENDOR_NAME:
            h = abs(java_hash(self.VENDOR_NAME)) & 0xFFFF
            return f"{h:04X}"
        return self.VENDOR_ID  # Fallback: hardcoded VENDOR_ID

    def _generate_serial(self, uid_bytes):
        if len(uid_bytes) >= 3:
            return "".join(f"{b:02X}" for b in uid_bytes[-3:])
        return "000001"

    # ── Creality-Nutzlast zusammenbauen (Plaintext, 20 Byte) ─────────────────

    def _build_plaintext(self, uid_bytes, material, color, empty_w, full_w):
        """
        Baut den 40-Zeichen Tag-String (ASCII Hex-Nibbles).
        Format (community reverse engineering DnG-Crafts/K2-RFID):
          date(5) + vendorId(4) + batch(2) + filamentId(6) + "0"+color(6) + length(4) + serial(6) + "000000"
          = 5 + 4 + 2 + 6 + 7 + 4 + 6 + 6 = 40 Zeichen

        Beispiel echte Creality-Tags:
          "AB1240276A21010010FFFFFF0165000001000000"
           AB124  = Datum: Monat=A=Okt, Tag=B1(BCD?), Jahr=24
           0276   = VendorID (Creality)
           A2     = Batch
           101001 = PLA
           0FFFFFF= Weiss
           0165   = 500g (357 dez = 0x165)
           000001 = Seriennummer
           000000 = Reserve

        v2.11 FIX: Nutzt _encode_date() fuer echtes Datum statt festem "AB124" Prefix.
        """
        date_str  = self._encode_date()                   # 5 Zeichen (Datum)
        vendor_id = self._generate_vendor_id()            # 4 Zeichen
        batch     = "A2"                                   # 2 Zeichen
        mat_id    = self._resolve_material_id(material)   # 6 Zeichen
        color_hex = "0" + self._resolve_color_hex(color)  # 7 Zeichen (Padding-0 + RRGGBB)
        length    = self._resolve_weight_bucket(full_w, empty_w)  # 4 Zeichen
        serial    = self._generate_serial(uid_bytes)      # 6 Zeichen
        reserve   = "000000"                              # 6 Zeichen

        tag_str = date_str + vendor_id + batch + mat_id + color_hex + length + serial + reserve
        assert len(tag_str) == 40, f"Tag-String falsche Laenge: {len(tag_str)} != 40"
        log.debug(f"   Plaintext aufgebaut: {tag_str}")
        log.debug(f"   Datum={date_str} Vendor={vendor_id} Batch={batch} "
                  f"FilID={mat_id} Farbe={color_hex} Len={length} Serial={serial}")
        return tag_str  # 40-Zeichen ASCII-String

    # ── AES-Verschlüsselung ───────────────────────────────────────────────────

    def _aes_ecb_encrypt(self, plaintext_96):
        """
        Verschluesselt die ersten 48 Byte mit AES-128-ECB (NoPadding).
        Key = AES_MASTER_KEY
        Input:  96 Byte (space-padded tagData als UTF-8)
        Output: 48 Byte Ciphertext (Sector 1, Blocks 4-6)

        Quelle: Utils.java cipherData() + WriteTag() in CFTag Android App:
          String paddedData = String.format("%-96s", tagData);
          byte[] s1Raw = Arrays.copyOfRange(fullDataBytes, 0, 48);
          byte[] s1ToDisk = cipherData(1, s1Raw);  // AES/ECB/NoPadding
        """
        if not AES_AVAILABLE:
            raise RuntimeError("pycryptodome nicht verfuegbar")
        s1_raw = plaintext_96[:48]   # erste 48 Byte
        cipher = AES.new(self.AES_MASTER_KEY, AES.MODE_ECB)
        return cipher.encrypt(s1_raw)  # 48 Byte Ciphertext

    def _derive_sector_key(self, uid_bytes):
        """
        Leitet den MIFARE Sector-1-Key (6 Byte) aus der Tag-UID ab.
        Methode: AES-128-ECB(SECTOR_KEY_SECRET, uid_hex_repeated_4x)[:6]
        Die UID wird als Hex-String 4x wiederholt → 32 Hex-Zeichen = 16 Bytes Plaintext.
        Verifiziert gegen 3 echte Creality-Tags am 2026-03-13.
        """
        if not AES_AVAILABLE:
            raise RuntimeError("pycryptodome nicht verfügbar")
        # UID als Hex-String 4x wiederholen → 16 Bytes
        uid_repeated = (uid_bytes * 4)[:16]  # 4 raw bytes * 4 = 16 Byte
        cipher = AES.new(self.SECTOR_KEY_SECRET, AES.MODE_ECB)
        encrypted = cipher.encrypt(uid_repeated)
        return encrypted[:6]  # Erste 6 Byte = Sector Key A

    # ── Hauptfunktion: Tag schreiben ──────────────────────────────────────────

    def write_creality_tag(self, uid_bytes, material, color, empty_w, full_w):
        """
        Schreibt einen Creality CFS-kompatiblen (verschluesselten) MIFARE Classic 1K Tag.

        Schritte:
          1. Nutzlast aufbauen (40 Zeichen Plaintext, echtes Datum via _encode_date)
          2. 96-Byte Buffer: Plaintext space-padded (wie Java "%-96s")
          3. AES-128-ECB: erste 48 Byte verschluesseln → Ciphertext fuer Sector 1
          4. Sector-Key aus UID ableiten
          5. Authentifizieren (Fabrikneu=FFFFFF, Re-Write=SectorKey via KeyA oder KeyB)
          6. Blocks 4,5,6 mit Ciphertext beschreiben
          7. Block 7 (Sector Trailer): KeyA=SectorKey | AccessBits | KeyB=SectorKey
             (v2.11 FIX: war vorher KeyA=000000 – K2+ konnte Tag nicht lesen!)
          8. Sector 2 (Blocks 8,9,10) unverschluesselt mit Space-Bytes beschreiben
        """
        try:
            # Schritt 1+2: Plaintext aufbauen und space-padden
            tag_str        = self._build_plaintext(uid_bytes, material, color, empty_w, full_w)
            tag_data_str   = tag_str.ljust(96)             # 96 ASCII-Zeichen (wie Java "%-96s")
            tag_data_bytes = tag_data_str.encode('utf-8')  # 96 Bytes
            assert len(tag_data_bytes) == 96

            # Schritt 3: AES/ECB – erste 48 Byte verschluesseln
            ciphertext = self._aes_ecb_encrypt(tag_data_bytes)  # 48 Byte
            assert len(ciphertext) == 48
            s2_plain = tag_data_bytes[48:96]  # Sector 2: 48 Byte unverschluesselt

            # Schritt 4: Sector-Key ableiten
            sector_key = self._derive_sector_key(uid_bytes)  # 6 Byte

            # v2.11 FIX: Trailer = KeyA=SectorKey | AccessBits | KeyB=SectorKey
            # VORHER (v2.10) war KeyA=000000, was den CFS-Reader blockiert hat.
            # Echte Creality-Tags haben KeyA=SectorKey damit der K2+ authentifizieren kann.
            trailer = bytearray(sector_key + self.SECTOR_TRAILER_ACCESS + sector_key)
            assert len(trailer) == 16

            log.info(f"🏷️  Creality CFS v2.11 (verschluesselt)")
            log.info(f"   Material: {material} → {self._resolve_material_id(material)}")
            log.info(f"   Farbe:    {color} → #{self._resolve_color_hex(color)}")
            log.info(f"   Gewicht:  {full_w}g (leer={empty_w}g) "
                     f"→ Bucket {self._resolve_weight_bucket(full_w, empty_w)}")
            log.info(f"   Ciphertext (48B): {ciphertext.hex().upper()}")
            log.info(f"   Sector Key (6B):  {sector_key.hex().upper()}")
            log.info(f"   Trailer (16B):    {trailer.hex().upper()}")

            # Raw-Frame Authentifizierung: kein SAM_configuration, kein Re-Read noetig.
            # Der Tag ist nach read_passive_target bereits selektiert (Tg=1).
            # Wir senden InDataExchange direkt – umgeht die Adafruit-Library komplett.
            time.sleep(0.1)

            AUTH_A = 0x60  # MIFARE_CMD_AUTH_A
            AUTH_B = 0x61  # MIFARE_CMD_AUTH_B
            factory_key = b'\xFF\xFF\xFF\xFF\xFF\xFF'

            # ── Sector 1 schreiben (Blocks 4, 5, 6, 7) ───────────────────────
            if self._raw_authenticate(uid_bytes, 4, AUTH_A, factory_key):
                log.info("   Sector 1 Auth: KeyA=FFFFFF (fabrikneu) [raw]")
                if not self._write_sector1_blocks(ciphertext, trailer):
                    return False

            elif self._raw_authenticate(uid_bytes, 4, AUTH_A, sector_key):
                log.info("   Sector 1 Auth: KeyA=SectorKey (v2.11-Tag) [raw]")
                if not self._write_sector1_blocks(ciphertext, trailer):
                    return False

            elif self._raw_authenticate(uid_bytes, 4, AUTH_B, sector_key):
                log.info("   Sector 1 Auth: KeyB=SectorKey (v2.10-Tag) [raw]")
                if not self._write_sector1_blocks(ciphertext, trailer):
                    return False

            else:
                log.error("❌ Sector 1 Authentifizierung fehlgeschlagen (alle Keys versucht)")
                log.error("   Versuche: KeyA=FFFFFF, KeyA=SectorKey, KeyB=SectorKey")
                return False

            # ── Sector 2 schreiben (Blocks 8, 9, 10) – unverschluesselt ──────
            s2_auth_ok = False
            if self._raw_authenticate(uid_bytes, 8, AUTH_A, factory_key):
                log.info("   Sector 2 Auth: KeyA=FFFFFF [raw]")
                s2_auth_ok = True
            elif self._raw_authenticate(uid_bytes, 8, AUTH_A, sector_key):
                log.info("   Sector 2 Auth: KeyA=SectorKey [raw]")
                s2_auth_ok = True
            else:
                log.warning("   ⚠️  Sector 2 Auth fehlgeschlagen")

            if s2_auth_ok:
                for block_num, offset in [(8, 0), (9, 16), (10, 32)]:
                    block_data = bytes(s2_plain[offset:offset + 16])
                    if not self._raw_write_block(block_num, block_data):
                        log.warning(f"   ⚠️  Block {block_num} (Sector 2) fehlgeschlagen")
                    else:
                        log.info(f"   Block {block_num}: {block_data.hex().upper()} (Sector 2)")

            log.info(f"✅ Creality CFS Tag (v2.11) fertig: {material}/{color} {full_w}g")
            return True

        except AssertionError as e:
            log.error(f"Tag-Format Fehler: {e}")
            return False
        except Exception as e:
            log.error(f"NFC write error: {e}")
            return False

    def _write_sector1_blocks(self, ciphertext, trailer):
        """
        Schreibt Blocks 4, 5, 6 (Ciphertext) und Block 7 (Trailer) via Raw-Frames.
        Muss nach erfolgreicher _raw_authenticate aufgerufen werden.
        """
        for block_num, offset in [(4, 0), (5, 16), (6, 32)]:
            block_data = bytes(ciphertext[offset:offset + 16])
            if not self._raw_write_block(block_num, block_data):
                log.error(f"❌ Block {block_num} (Ciphertext) schreiben fehlgeschlagen")
                return False
            log.info(f"   Block {block_num}: {block_data.hex().upper()}")

        # Block 7 = Sector Trailer (KeyA=SectorKey, AccessBits, KeyB=SectorKey)
        if not self._raw_write_block(7, bytes(trailer)):
            log.warning("   ⚠️  Block 7 (Sector Trailer) fehlgeschlagen")
        else:
            log.info(f"   Block  7: {bytes(trailer).hex().upper()} (Trailer)")
        return True

    # Alias fuer Rueckwaertskompatibilitaet
    def write_block1(self, uid_bytes, material, color, empty_w, full_w):
        """Alias fuer write_creality_tag() – Rueckwaertskompatibilitaet."""
        return self.write_creality_tag(uid_bytes, material, color, empty_w, full_w)


class ServerAPI:
    def __init__(self):
        self.ok = False
        self._check_server()
    
    def _check_server(self):
        try:
            r = requests.get(f"{SERVER_URL}/api/ping", timeout=2)
            self.ok = r.status_code == 200
            if self.ok:
                log.info("✅ Server erreichbar")
        except:
            self.ok = False
            log.warning("⚠️  Server nicht erreichbar")
    
    def spool_detect(self, uid, weight):
        try:
            r = requests.post(f"{SERVER_URL}/api/spool_detect",
                json={"uid": uid, "weight": weight}, timeout=2)
            return r.json() if r.ok else {}
        except Exception as e:
            log.debug(f"Server error: {e}")
            return {}
    
    def notify_nfc_written(self, uid):
        try:
            requests.post(f"{SERVER_URL}/api/nfc_sync",
                json={"uid": uid, "action": "block1_written"}, timeout=2)
        except:
            pass
    
    def poll_command(self):
        """
        NEU v2.5: Server auf Befehle abfragen
        Returns: {"command": "tare"|"calibrate"|"reboot"|"shutdown"|None}
        """
        try:
            r = requests.get(f"{SERVER_URL}/api/command/poll", timeout=2)
            if r.ok:
                data = r.json()
                return data.get("command")
        except Exception as e:
            log.debug(f"Command poll error: {e}")
        return None
    
    def ack_command(self, command, status, message=""):
        """
        NEU v2.5: Befehlsausführung bestätigen
        """
        try:
            requests.post(f"{SERVER_URL}/api/command/ack",
                json={"command": command, "status": status, "message": message},
                timeout=2)
        except:
            pass


class FilamentWaage:
    def __init__(self, cfg):
        self.cfg      = cfg
        self.running  = True
        self.display  = OLEDDisplay()
        self.display.show_boot("Waage init...")
        self.waage    = Waage(cfg)
        self.display.show_boot("NFC init...")
        self.nfc      = NFC()
        self.display.show_boot("Server verbinden...")
        self.server   = ServerAPI()
        
        self.current_uid     = None
        self._last_uid_time  = 0
        self._last_w_update  = 0
        self._last_disp_w    = 0
        self._last_cmd_poll  = 0  # NEU: Letzter Command-Poll
        self._spool_cache    = {}
    
    def _do_calibration(self):
        log.info("Starte Kalibrierung")

        # Schritt 1: Tare – Waage muss LEER sein
        self.display.show_calibration("Schritt 1/2", "Waage leeren!")
        time.sleep(5)   # 5 Sekunden Zeit um alles zu entfernen
        self.waage.tare()
        self.cfg["calibration"]["zero_baseline"] = round(self.waage.baseline, 1)
        save_config(self.cfg)
        log.info(f"Tare gesetzt: {self.waage.baseline:.1f}")

        # Schritt 2: 500g auflegen – genug Zeit lassen!
        self.display.show_calibration("Schritt 2/2", "500g auflegen!")
        log.info("Warte 8 Sekunden – bitte 500g auflegen...")
        time.sleep(8)   # 8 Sekunden Zeit um Gewicht aufzulegen
        f = self.waage.calibrate_500g()
        if f and f > 0:
            self.cfg["calibration"]["gram_factor"] = round(f, 4)
            self.cfg["calibration"]["zero_baseline"] = round(self.waage.baseline, 1)
            save_config(self.cfg)
            log.info(f"Kalibrierung OK: Faktor={f:.4f}")
            self.display.show_msg("KALIBRIERUNG OK", f"Faktor: {f:.2f}", duration=3)
        else:
            self.display.show_msg("FEHLER", "Gewicht auflegen!", duration=3)
            log.error("Kalibrierung fehlgeschlagen – war das 500g Gewicht auf der Waage?")

    def _register_masters(self):
        log.info("Starte Master-Tag Registrierung")
        tags = [
            ("tare", "TARE"),
            ("calibrate", "KALIBRIERUNG"),
            ("reboot", "NEUSTART"),
            ("shutdown", "SHUTDOWN"),
        ]
        for step, (key, name) in enumerate(tags, 1):
            self.display.show_register(name, step, len(tags))
            deadline = time.time() + 30
            while time.time() < deadline:
                uid, _ = self.nfc.read()
                if uid and uid not in self.cfg["master_tags"].values():
                    self.cfg["master_tags"][key] = uid
                    save_config(self.cfg)
                    log.info(f"Master registriert: {key} = {uid}")
                    self.display.show_msg(name, uid[:12], duration=1.5)
                    while self.nfc.read()[0]:
                        time.sleep(0.1)
                    break
                time.sleep(0.15)
        self.cfg["registered"] = True
        save_config(self.cfg)
        self.display.show_msg("SETUP OK", "Bereit!", duration=2)

    def _handle_spool(self, uid, uid_bytes, weight):
        resp = self.server.spool_detect(uid, weight)
        
        if resp.get("status") == "new_spool":
            log.info(f"Neue Spule: {uid}")
            self.display.show_new_spool(uid)
        
        elif resp.get("status") == "ok":
            mat = resp.get("material", "?")
            col = resp.get("color", "?")
            ew  = resp.get("empty_weight", 220)
            fw  = resp.get("full_weight", 1220)
            log.info(f"Spule bekannt: {uid} {mat}/{col} {weight:.1f}g")
            self.display.show_spool(uid, mat, col, weight, ew, fw)
            
            cached = self._spool_cache.get(uid, {})
            if (cached.get("empty_weight") != ew or cached.get("full_weight") != fw):
                log.info(f"🔄 NFC Update nötig für {uid}")
                # uid_bytes direkt aus der Hauptschleife – kein zweites read_passive_target!
                success = self.nfc.write_block1(uid_bytes, mat, col, ew, fw)
                if success:
                    self.display.show_nfc_write(uid)
                    self.server.notify_nfc_written(uid)
                else:
                    log.warning(f"⚠️  NFC Schreiben fehlgeschlagen für {uid}")
            
            self._spool_cache[uid] = {"empty_weight": ew, "full_weight": fw}
        
        else:
            log.warning(f"Server-Fehler fuer {uid}")
            self.display.show_main(weight=weight, status=uid[:10], server_ok=self.server.ok)
    
    def _execute_master_command(self, command):
        """
        NEU v2.5: Master-Befehl ausführen
        """
        log.info(f"🎛️ Master-Command: {command}")
        
        try:
            if command == "tare":
                self.display.show_msg("TARE", "Nullstellung...")
                self.waage.tare()
                self.cfg["calibration"]["zero_baseline"] = round(self.waage.baseline, 1)
                save_config(self.cfg)
                self.server.ack_command(command, "ok", "Tare erfolgreich")
                time.sleep(1)
            
            elif command == "calibrate":
                self._do_calibration()
                self.server.ack_command(command, "ok", "Kalibrierung abgeschlossen")
            
            elif command == "shutdown":
                self.display.show_msg("SHUTDOWN", "Bitte warten...", duration=0)
                self.server.ack_command(command, "ok", "Shutdown initiiert")
                time.sleep(5)  # Pi sauber runterfahren lassen
                self.display.show_msg("Jetzt ausstecken!", "", duration=0)
                time.sleep(0.5)  # OLED Zeit zum Anzeigen geben
                os.system("sudo shutdown -h now")
            
            elif command == "reboot":
                self.display.show_msg("REBOOT", "...")
                self.server.ack_command(command, "ok", "Reboot initiiert")
                time.sleep(2)
                os.system("sudo reboot")
        
        except Exception as e:
            log.error(f"Command error: {e}")
            self.server.ack_command(command, "error", str(e))

    def run(self):
        if not self.cfg.get("registered"):
            self._register_masters()
        self.display.show_main(server_ok=self.server.ok)
        log.info("🔄 Hauptschleife gestartet - Command Polling aktiviert")
        
        while self.running:
            now = time.time()
            
            # NEU: Server-Befehle abfragen (alle 2 Sekunden)
            if now - self._last_cmd_poll > 2.0:
                self._last_cmd_poll = now
                cmd = self.server.poll_command()
                if cmd:
                    self._execute_master_command(cmd)
                    # Nach Befehlsausführung Display zurücksetzen
                    self.current_uid = None
                    self.display.show_main(server_ok=self.server.ok)
                    continue
            
            # Gewichtsupdate
            if now - self._last_w_update > 0.3:
                w = self.waage.grams()
                self._last_w_update = now
                if self.current_uid is None and abs(w - self._last_disp_w) > 2.0:
                    self.display.update_weight(w)
                    self._last_disp_w = w

            # NFC-Tag Handling
            uid, uid_bytes = self.nfc.read()
            if uid:
                # Master-Tags über physische Tags (optional, parallel zu Server)
                if uid in self.cfg["master_tags"].values():
                    for k, v in self.cfg["master_tags"].items():
                        if v == uid:
                            self._execute_master_command(k)
                            while self.nfc.read()[0]:
                                time.sleep(0.1)
                            self.current_uid = None
                            self.display.show_main(server_ok=self.server.ok)
                            break
                else:
                    if self.current_uid != uid or now - self._last_uid_time > 5.0:
                        self.current_uid = uid
                        self._last_uid_time = now
                        w = self.waage.grams()
                        self._handle_spool(uid, uid_bytes, w)
            else:
                if self.current_uid and now - self._last_uid_time > 1.5:
                    self.current_uid = None
                    self.display.show_main(server_ok=self.server.ok)

            time.sleep(0.15)


def main():
    log.info("=" * 60)
    log.info("FilaStation v2.12 - HX711 Fix (invertiert) + Creality CFS NFC + Server Command Integration")
    log.info("OLED Software-I2C + Remote Master-Steuerung + Creality K2+ CFS Encrypted Support")
    log.info("=" * 60)
    
    cfg = load_config()
    # SERVER_URL aus Config laden (überschreibt den Fallback-Wert)
    global SERVER_URL
    SERVER_URL = cfg.get("server_url", SERVER_URL)
    log.info(f"🌐 Server-URL: {SERVER_URL}")
    app = FilamentWaage(cfg)
    
    try:
        app.run()
    except KeyboardInterrupt:
        log.info("\n⚠️  Programm beendet")
    finally:
        app.display.close()
        log.info("👋 Shutdown")


if __name__ == "__main__":
    main()
