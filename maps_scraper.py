"""
Google Maps Scraper → Google Sheets
Extrae negocios de Google Maps y los guarda en la pestaña "Negocios".

Uso:
  python maps_scraper.py
  python maps_scraper.py --localidad "San Isidro" --radio 30 --query "muebles"
  python maps_scraper.py --headless
"""

import argparse
import time
import random
import re
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import sync_playwright, Page
import gspread
from google.oauth2.service_account import Credentials


# ── CONFIGURACIÓN ─────────────────────────────────────────────────────────────
SPREADSHEET_ID   = "1AqoQn6c_r4bqObBEa5W5EASP6lvrqERJzZbF2MnbRCE"
SHEET_NAME       = "Negocios"
CREDENTIALS_FILE = "credentials.json"   # Service Account key (ver README)

HEADERS = [
    "Nombre", "Dirección", "Teléfono",
    "Sitio Web", "Instagram", "Facebook",
    "Reseñas", "Categoría", "Maps URL",
]

# ── MODELO DE DATOS ───────────────────────────────────────────────────────────
def limpiar(valor: str) -> str:
    """Elimina caracteres especiales ( ) y saltos de línea de los valores."""
    return re.sub(r"[\(\)\n\r]", "", valor).strip()

@dataclass
class Negocio:
    nombre:    str = ""
    direccion: str = ""
    telefono:  str = ""
    sitio_web: str = ""
    instagram: str = ""
    facebook:  str = ""
    resenias:  str = ""
    categoria: str = ""
    maps_url:  str = ""

    def to_row(self) -> list:
        return [limpiar(v) for v in [
            self.nombre, self.direccion, self.telefono,
            self.sitio_web, self.instagram, self.facebook,
            self.resenias, self.categoria, self.maps_url,
        ]]


# ── HELPERS ───────────────────────────────────────────────────────────────────
def delay(min_s: float = 1.5, max_s: float = 3.5):
    """Pausa aleatoria para evitar detección."""
    time.sleep(random.uniform(min_s, max_s))


def safe_text(page: Page, selector: str, timeout: int = 3000) -> str:
    """Extrae texto de un selector sin lanzar excepción si no existe."""
    try:
        return page.locator(selector).first.inner_text(timeout=timeout).strip()
    except Exception:
        return ""


def safe_attr(page: Page, selector: str, attr: str, timeout: int = 3000) -> str:
    """Extrae un atributo de un selector sin lanzar excepción si no existe."""
    try:
        return page.locator(selector).first.get_attribute(attr, timeout=timeout) or ""
    except Exception:
        return ""


# ── SCRAPER ───────────────────────────────────────────────────────────────────
class GoogleMapsScraper:

    def __init__(self, headless: bool = False):
        self.headless = headless

    def buscar(self, localidad: str, radio_km: int, query: str) -> list[Negocio]:
        negocios: list[Negocio] = []
        search_term = f"{query} cerca de {localidad}, Buenos Aires, Argentina"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="es-AR",
            )
            page = context.new_page()

            print(f"\n🔍 Buscando: '{search_term}' (radio ~{radio_km} km)\n")
            page.goto("https://www.google.com/maps", wait_until="load", timeout=60000)
            delay(2, 3)

            # Screenshot para debug — abrí debug.png para ver qué está viendo el script
            page.screenshot(path="debug.png")
            print("📸 Screenshot guardado en debug.png — verificá qué muestra el browser.")

            # Manejar pantalla de consentimiento de Google (aparece antes de Maps)
            for selector, action in [
                ("button[aria-label='Rechazar todo']",   "click"),
                ("button[aria-label='Reject all']",      "click"),
                ("button[aria-label='Accept all']",      "click"),
                ("form[action*='consent'] button",       "click"),
                ("#L2AGLb",                              "click"),  # "Acepto" clásico
            ]:
                try:
                    page.wait_for_selector(selector, timeout=2000)
                    page.click(selector)
                    print(f"  ✅ Diálogo cerrado: {selector}")
                    delay(1, 2)
                    break
                except Exception:
                    pass

            # Esperar el campo de búsqueda con múltiples selectores posibles
            search_input = None
            for selector in ["input#searchboxinput", "input[name='q']", "input[aria-label*='earch']"]:
                try:
                    page.wait_for_selector(selector, timeout=10000)
                    search_input = selector
                    print(f"  ✅ Campo de búsqueda encontrado: {selector}")
                    break
                except Exception:
                    continue

            if not search_input:
                page.screenshot(path="debug_no_input.png")
                raise RuntimeError("No se encontró el campo de búsqueda. Revisá debug_no_input.png.")

            # Ejecutar búsqueda
            page.click(search_input)
            delay(0.5, 1)
            page.fill(search_input, search_term)
            delay(0.5, 1)
            page.keyboard.press("Enter")
            page.wait_for_selector("div[role='feed']", timeout=20000)
            delay(2, 3)

            # Scroll y recolección de URLs
            urls = self._scroll_y_recolectar_urls(page, radio_km)
            print(f"📋 {len(urls)} resultados encontrados. Extrayendo detalles...\n")

            # Extraer detalle de cada resultado
            for i, url in enumerate(urls, 1):
                try:
                    print(f"  [{i:>3}/{len(urls)}] Procesando...", end=" ", flush=True)
                    negocio = self._extraer_detalle(page, url, localidad)
                    if negocio:
                        negocios.append(negocio)
                        print(f"✅ {negocio.nombre}")
                    else:
                        print("⏭  Sin nombre, omitido.")
                    delay(2, 4)
                except Exception as e:
                    print(f"⚠️  Error: {e}")
                    continue

            browser.close()

        return negocios

    # ── Scroll en el panel de resultados ──────────────────────────────────────
    def _scroll_y_recolectar_urls(self, page: Page, radio_km: int) -> list[str]:
        urls: set[str] = set()
        feed = page.locator("div[role='feed']")
        max_scrolls = max(15, radio_km // 4)

        for _ in range(max_scrolls):
            # Recolectar links visibles en el panel
            for link in page.locator("a[href*='/maps/place/']").all():
                href = link.get_attribute("href")
                if href:
                    # Normalizar: quedarse solo con la URL base del lugar
                    base = href.split("?")[0]
                    urls.add(base)

            # Detectar fin de resultados
            if page.locator("p[class*='fontBodyMedium'] span").filter(
                has_text="Has llegado al final"
            ).count() > 0:
                print("  📌 Fin de la lista de resultados.")
                break

            feed.evaluate("el => el.scrollBy(0, 1500)")
            delay(1.5, 2.5)

        return list(urls)

    # ── Extracción de detalle de un negocio ───────────────────────────────────
    def _extraer_detalle(self, page: Page, url: str, localidad_busqueda: str) -> Optional[Negocio]:
        page.goto(url, wait_until="domcontentloaded")
        delay(2, 3)

        negocio = Negocio(maps_url=url)

        # Nombre (obligatorio)
        negocio.nombre = safe_text(page, "h1")
        if not negocio.nombre:
            return None

        # Categoría
        negocio.categoria = safe_text(page, "button[jsaction*='category']")

        # Cantidad de reseñas
        raw_res = safe_text(page, "button[aria-label*='reseña']")
        negocio.resenias = re.sub(r"[^\d]", "", raw_res)

        # Dirección
        negocio.direccion = safe_text(page, "button[data-item-id='address']")

        # Teléfono
        negocio.telefono = safe_text(page, "button[data-item-id^='phone']")

        # Sitio web
        negocio.sitio_web = safe_attr(page, "a[data-item-id='authority']", "href")

        # Redes sociales — links directos en el perfil de Maps
        try:
            for link in page.locator("a[href*='instagram.com'], a[href*='facebook.com']").all():
                href = link.get_attribute("href") or ""
                if "instagram.com" in href and not negocio.instagram:
                    negocio.instagram = href.rstrip("/")
                if "facebook.com" in href and not negocio.facebook:
                    negocio.facebook = href.rstrip("/")
        except Exception:
            pass

        return negocio


# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
class SheetsExporter:

    def __init__(self):
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        self.client = gspread.authorize(creds)

    def exportar(self, negocios: list[Negocio]):
        sh = self.client.open_by_key(SPREADSHEET_ID)
        ws = sh.worksheet(SHEET_NAME)

        # Escribir encabezados si la hoja está vacía o desactualizada
        primera_fila = ws.row_values(1)
        if primera_fila != HEADERS:
            ws.clear()
            ws.append_row(HEADERS)
            print("\n📝 Encabezados escritos en la hoja.")

        # Deduplicar: no agregar si (nombre + dirección) ya existen
        existentes = ws.get_all_values()[1:]  # skip header
        existentes_keys = {
            (r[0].strip().lower(), r[1].strip().lower())
            for r in existentes if len(r) >= 2
        }

        nuevas_filas = []
        for neg in negocios:
            key = (neg.nombre.strip().lower(), neg.direccion.strip().lower())
            if key not in existentes_keys:
                nuevas_filas.append(neg.to_row())
                existentes_keys.add(key)

        if nuevas_filas:
            ws.append_rows(nuevas_filas, value_input_option="USER_ENTERED")
            print(f"✅ {len(nuevas_filas)} negocios nuevos exportados a Google Sheets.")
        else:
            print("ℹ️  No hay negocios nuevos para agregar (todos ya existen en la hoja).")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Scraper Google Maps → Google Sheets",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--localidad", type=str, default="Bella Vista",
                        help="Localidad central de búsqueda")
    parser.add_argument("--radio",     type=int, default=50,
                        help="Radio de búsqueda aproximado en km")
    parser.add_argument("--query",     type=str, default="muebles",
                        help="Tipo de negocio a buscar")
    parser.add_argument("--headless",  action="store_true",
                        help="Correr el browser sin ventana gráfica")
    args = parser.parse_args()

    scraper  = GoogleMapsScraper(headless=args.headless)
    negocios = scraper.buscar(args.localidad, args.radio, args.query)

    print(f"\n📦 Total extraídos: {len(negocios)}")

    if negocios:
        exporter = SheetsExporter()
        exporter.exportar(negocios)
    else:
        print("⚠️  No se encontraron negocios. Verificá la búsqueda o el radio.")


if __name__ == "__main__":
    main()