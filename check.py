"""
Vigilante de disponibilidad — Cenacolo Vinciano (Última Cena, Milán)

Qué hace:
1. Abre la página de reserva con un navegador headless (Playwright).
2. Busca en la página los días objetivo (ej: 7, 8, 9, 10, 11 de septiembre).
3. Comprueba si alguno de esos días parece "disponible" en vez de "agotado/deshabilitado".
4. Si detecta un cambio respecto a la última vez que se ejecutó, avisa por Telegram y email.
5. Guarda el estado actual en state.json para comparar la próxima vez.

CONFIGURACIÓN NECESARIA (variables de entorno / GitHub Secrets):
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
- GMAIL_USER          (tu email de Gmail)
- GMAIL_APP_PASSWORD  (contraseña de aplicación de Gmail, no tu contraseña normal)
- NOTIFY_EMAIL        (a qué email quieres que llegue el aviso, puede ser el mismo GMAIL_USER)

IMPORTANTE — TIENES QUE AJUSTAR ESTO:
- TARGET_URL: la URL de la página de reserva.
- TARGET_DAYS: los días del mes que te interesan (7 al 11 de septiembre = [7,8,9,10,11]).
- CALENDAR_SELECTOR: el selector CSS del contenedor del calendario en la página.
  Cómo encontrarlo:
    1. Abre la página de reserva en Chrome/Firefox.
    2. Clic derecho sobre el calendario -> "Inspeccionar".
    3. Busca el elemento contenedor que engloba todos los días (normalmente un <div> con
       clase tipo "calendar", "datepicker", "days-grid"...).
    4. Copia ese selector aquí abajo (ej: ".calendar-days" o "#booking-calendar").
  Si no lo encuentras a la primera, no pasa nada: el script por defecto analiza
  <body> entero buscando los números de día, es menos preciso pero funciona como red
  de seguridad (avisará ante cualquier cambio de texto relevante cerca de esos números).
"""

import os
import json
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# ---------------- CONFIGURACIÓN ----------------

TARGET_URL = "https://cenacolovinciano.vivaticket.it/it/event/cenacolo-vinciano/151991"

# Días del mes de septiembre 2026 que te interesan
TARGET_DAYS = [7, 8, 9, 10, 11]

# Selector real del calendario en la web del Cenacolo Vinciano (confirmado inspeccionando la página)
CALENDAR_SELECTOR = "#dayOfTheMonth_151991"

# Clases reales que usa la web:
#   "inactive"  -> sin plazas disponibles (title="Posti non disponibili")
#   "no-event"  -> día cerrado / sin visitas programadas (ej. lunes, o domingo gratuito)
# Si un día NO tiene ninguna de las dos, se interpreta como disponible.

STATE_FILE = Path(__file__).parent / "state.json"

# ---------------- LÓGICA ----------------


def fetch_calendar_snapshot() -> dict:
    """Abre la página con Playwright y devuelve el estado real de cada día objetivo."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))
        page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)  # margen extra para JS lento

        try:
            page.wait_for_selector(CALENDAR_SELECTOR, timeout=15000)
            html = page.locator(CALENDAR_SELECTOR).inner_html()
        except Exception:
            print(f"[aviso] no se encontró el selector {CALENDAR_SELECTOR}, analizando toda la página")
            html = page.content()

        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    snapshot = {}

    for li in soup.find_all("li"):
        text = li.get_text(strip=True)
        if not text.isdigit():
            continue
        day_num = int(text)
        if day_num not in TARGET_DAYS:
            continue

        classes = li.get("class", [])
        is_unavailable = "inactive" in classes
        is_closed = "no-event" in classes
        is_available = not is_unavailable and not is_closed

        snapshot[str(day_num)] = {
            "looks_unavailable": is_unavailable,
            "is_closed": is_closed,
            "is_available": is_available,
            "found": True,
        }

    # Marca como "no encontrado" cualquier día objetivo que no apareciera en el calendario actual
    for day in TARGET_DAYS:
        snapshot.setdefault(str(day), {
            "looks_unavailable": False, "is_closed": False, "is_available": False, "found": False,
        })

    return snapshot


def load_previous_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def detect_changes(previous: dict, current: dict) -> list:
    changes = []
    for day, info in current.items():
        prev_info = previous.get(day)
        if prev_info is None:
            continue  # primera vez que vemos este día, no es un "cambio"
        # Avisa si el día pasa de NO disponible a SÍ disponible
        if not prev_info.get("is_available") and info.get("is_available"):
            changes.append(day)
    return changes


def send_telegram(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[aviso] Telegram no configurado, se omite el envío")
        return
    import urllib.request
    import urllib.parse

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=15)
    except Exception as e:
        print(f"[error] fallo enviando Telegram: {e}")


def send_email(subject: str, body: str) -> None:
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
    to_addr = os.environ.get("NOTIFY_EMAIL", gmail_user)
    if not gmail_user or not gmail_pass:
        print("[aviso] Email no configurado, se omite el envío")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_addr
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, [to_addr], msg.as_string())
    except Exception as e:
        print(f"[error] fallo enviando email: {e}")


def main() -> None:
    previous = load_previous_state()
    current = fetch_calendar_snapshot()
    changes = detect_changes(previous, current)

    if changes:
        days_txt = ", ".join(changes)
        message = (
            f"🎨 ¡Posible hueco nuevo en el Cenacolo Vinciano!\n"
            f"Días con cambio detectado: {days_txt} de septiembre.\n"
            f"Revisa YA: {TARGET_URL}"
        )
        print(message)
        send_telegram(message)
        send_email("Cenacolo Vinciano — posible disponibilidad", message)
    else:
        print("Sin cambios detectados en esta pasada.")

    save_state(current)


if __name__ == "__main__":
    main()
