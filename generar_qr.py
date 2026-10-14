"""
Genera los QR de acceso al sistema SGI-Pasaportes.

Uso:
    python generar_qr.py

Produce dos archivos PNG listos para imprimir:
    qr_portal_web.png   → apunta al portal web (LAN)
    qr_telegram.png     → abre el bot de Telegram

Requiere: pip install qrcode[pil] Pillow
(No forma parte de requirements.txt porque solo se ejecuta en el PC local, no en Docker)
"""
import os
import socket
import subprocess
import sys

# ── instalar dependencias si no están ─────────────────────────────────────────
try:
    import qrcode
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Instalando dependencias (qrcode, Pillow)…")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "qrcode[pil]", "Pillow"])
    import qrcode
    from PIL import Image, ImageDraw, ImageFont

# ── leer token del bot desde .env para construir el enlace de Telegram ────────
def leer_env(clave: str) -> str:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return ""
    with open(env_path, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if linea.startswith(f"{clave}="):
                return linea.split("=", 1)[1].strip().strip('"').strip("'")
    return ""

# ── obtener IP local ──────────────────────────────────────────────────────────
def ip_local() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

# ── generar QR ────────────────────────────────────────────────────────────────
def generar_qr(url: str, titulo: str, subtitulo: str, nombre_archivo: str) -> None:
    # QR
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=12,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img_qr = qr.make_image(fill_color="#1e3a8a", back_color="white").convert("RGBA")

    # Lienzo con texto
    ancho_qr, alto_qr = img_qr.size
    margen = 30
    alto_texto = 120
    ancho_total = ancho_qr + margen * 2
    alto_total  = alto_qr + alto_texto + margen * 2

    lienzo = Image.new("RGB", (ancho_total, alto_total), "#f1f5f9")
    draw = ImageDraw.Draw(lienzo)

    # Fondo azul superior
    draw.rectangle([0, 0, ancho_total, alto_texto], fill="#1e3a8a")

    # Texto
    try:
        fuente_titulo   = ImageFont.truetype("arial.ttf", 28)
        fuente_subtitulo = ImageFont.truetype("arial.ttf", 18)
        fuente_url      = ImageFont.truetype("arial.ttf", 14)
    except Exception:
        fuente_titulo   = ImageFont.load_default()
        fuente_subtitulo = fuente_titulo
        fuente_url      = fuente_titulo

    draw.text((ancho_total // 2, 24), titulo, fill="white", font=fuente_titulo, anchor="mm")
    draw.text((ancho_total // 2, 60), subtitulo, fill="#93c5fd", font=fuente_subtitulo, anchor="mm")
    draw.text((ancho_total // 2, 90), url, fill="#cbd5e1", font=fuente_url, anchor="mm")

    # QR centrado
    x = (ancho_total - ancho_qr) // 2
    y = alto_texto + margen
    lienzo.paste(img_qr, (x, y))

    lienzo.save(nombre_archivo)
    print(f"✅  Guardado: {nombre_archivo}  ({url})")


# ── main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ip = ip_local()
    url_web = f"http://{ip}:8080"

    # Obtener username del bot desde BotFather (necesita TOKEN)
    token = leer_env("TELEGRAM_BOT_TOKEN")
    url_telegram = "https://t.me/TU_BOT"  # fallback
    if token:
        import urllib.request, json
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=5) as r:
                data = json.loads(r.read())
                if data.get("ok"):
                    username = data["result"].get("username", "")
                    if username:
                        url_telegram = f"https://t.me/{username}"
        except Exception:
            pass

    generar_qr(
        url=url_web,
        titulo="Reportar Falla de Equipo",
        subtitulo="Escanea con tu teléfono → Portal Web",
        nombre_archivo="qr_portal_web.png",
    )
    generar_qr(
        url=url_telegram,
        titulo="Reportar por Telegram",
        subtitulo="Escanea con tu teléfono → Bot de Soporte TI",
        nombre_archivo="qr_telegram.png",
    )

    print()
    print("-- Listo ------------------------------------------------")
    print(f"   Portal web:  {url_web}")
    print(f"   Telegram:    {url_telegram}")
    print()
    print("  Imprime ambos archivos PNG y pegaios en los modulos.")
    print("  Si la IP cambia, vuelve a ejecutar este script.")
    print("---------------------------------------------------------")
