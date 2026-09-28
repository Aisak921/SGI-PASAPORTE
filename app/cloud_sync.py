"""Sincronización directa con OneDrive Nube a través de la API de Microsoft Graph.

Funciona en desarrollo local y en servidores Linux (Oracle Cloud / VPS) sin necesidad de cliente de escritorio.

Uso de prueba / generación interactiva de Refresh Token:
  python -m app.cloud_sync
"""

import asyncio
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import urllib.parse
import webbrowser
import httpx

from .config import settings

log = logging.getLogger(__name__)


async def obtener_token_acceso() -> str | None:
    """Obtiene un Access Token válido de Microsoft Graph usando Refresh Token o Client Credentials."""
    if not settings.MICROSOFT_CLIENT_ID:
        log.warning("MICROSOFT_CLIENT_ID sin configurar; la sincronización API en la nube está desactivada.")
        return None

    tenant = settings.MICROSOFT_TENANT_ID or "common"
    url_token = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    data = {
        "client_id": settings.MICROSOFT_CLIENT_ID,
    }
    if settings.MICROSOFT_CLIENT_SECRET:
        data["client_secret"] = settings.MICROSOFT_CLIENT_SECRET

    if settings.MICROSOFT_REFRESH_TOKEN:
        data["grant_type"] = "refresh_token"
        data["refresh_token"] = settings.MICROSOFT_REFRESH_TOKEN
        data["scope"] = "offline_access files.readwrite"
    else:
        data["grant_type"] = "client_credentials"
        data["scope"] = "https://graph.microsoft.com/.default"

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.post(url_token, data=data)
            if resp.status_code == 200:
                json_resp = resp.json()
                return json_resp.get("access_token")
            else:
                log.error("Error al obtener token de Microsoft Graph (%s): %s", resp.status_code, resp.text)
                return None
        except Exception as e:
            log.exception("Excepción conectando a Microsoft Graph OAuth: %s", e)
            return None


async def subir_a_n8n_webhook(ruta_local: str | Path, nombre_remoto: str | None = None) -> bool:
    """Envía el archivo .xlsx generado a un Webhook de n8n para que n8n lo sobrescriba/actualice en OneDrive/Google Drive."""
    if not settings.N8N_WEBHOOK_URL:
        return False

    ruta_p = Path(ruta_local)
    if not ruta_p.exists():
        log.error("El archivo local %s no existe para enviar a n8n.", ruta_local)
        return False

    nombre_archivo = nombre_remoto or ruta_p.name
    contenido = ruta_p.read_bytes()

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            files = {"file": (nombre_archivo, contenido, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
            data = {"filename": nombre_archivo}
            resp = await client.post(settings.N8N_WEBHOOK_URL, data=data, files=files)
            if resp.status_code in (200, 201, 202, 204):
                log.info("[CLOUD API - N8N] Archivo %s enviado exitosamente al Webhook de n8n", nombre_archivo)
                return True
            else:
                log.error("[CLOUD API - N8N] Falló el envío al Webhook n8n (%s): %s", resp.status_code, resp.text)
                return False
        except Exception as e:
            log.exception("[CLOUD API - N8N] Excepción enviando archivo a n8n Webhook: %s", e)
            return False


async def subir_archivo_a_onedrive(ruta_local: str | Path, nombre_remoto: str | None = None) -> bool:
    """Sube/actualiza un archivo .xlsx en la nube (vía n8n Webhook o Microsoft Graph API).
    
    Devuelve True si la subida a la nube fue exitosa, o False si falló/está desactivada.
    """
    if not settings.ENABLE_CLOUD_API_SYNC:
        return False

    # 1. Si está configurado un Webhook de n8n, priorizamos n8n (ultrasencillo y 0 problemas con Azure)
    if settings.N8N_WEBHOOK_URL:
        return await subir_a_n8n_webhook(ruta_local, nombre_remoto)

    # 2. De lo contrario, usamos la API directa de Microsoft Graph
    token = await obtener_token_acceso()
    if not token:
        log.warning("No se obtuvo token de acceso a la nube. Sincronización API omitida.")
        return False

    ruta_p = Path(ruta_local)
    if not ruta_p.exists():
        log.error("El archivo local %s no existe para subir a OneDrive.", ruta_local)
        return False

    nombre_archivo = nombre_remoto or ruta_p.name
    folder_remoto = settings.ONEDRIVE_REMOTE_FOLDER.strip("/")
    url_put = f"https://graph.microsoft.com/v1.0/me/drive/root:/{folder_remoto}/{nombre_archivo}:/content"

    contenido = ruta_p.read_bytes()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.put(url_put, content=contenido, headers=headers)
            if resp.status_code in (200, 201):
                log.info("[CLOUD API] Archivo %s subido/actualizado exitosamente en OneDrive (%s)", nombre_archivo, folder_remoto)
                return True
            else:
                log.error("[CLOUD API] Falló la subida a OneDrive (%s): %s", resp.status_code, resp.text)
                return False
        except Exception as e:
            log.exception("[CLOUD API] Excepción al subir archivo a OneDrive: %s", e)
            return False


# ---------------------------------------------------------------- Generador interactivo de Refresh Token
_auth_code_global = None

class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code_global
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if "code" in query:
            _auth_code_global = query["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h1>Autenticación exitosa!</h1><p>Puedes cerrar esta ventana y regresar a la terminal.</p>".encode("utf-8"))
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def generar_refresh_token_interactivo():
    client_id = settings.MICROSOFT_CLIENT_ID
    if not client_id:
        print("ERROR: Primero debes configurar MICROSOFT_CLIENT_ID en tu archivo .env")
        return

    redirect_uri = "http://localhost:8400/callback"
    scope = "offline_access files.readwrite"
    tenant = settings.MICROSOFT_TENANT_ID or "common"

    auth_url = (
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?"
        f"client_id={client_id}&response_type=code&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&scope={urllib.parse.quote(scope)}"
    )

    print("\n--- Generador de Refresh Token de Microsoft Graph ---")
    print("1. Abriendo la URL de inicio de sesión en tu navegador...")
    print(f"URL: {auth_url}\n")

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    server = HTTPServer(("localhost", 8400), _CallbackHandler)
    print("Esperando la confirmación de inicio de sesión en el puerto 8400...")
    while _auth_code_global is None:
        server.handle_request()

    code = _auth_code_global
    print("\nCódigo de autorización recibido. Intercambiando por Refresh Token...")

    url_token = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": scope,
    }
    if settings.MICROSOFT_CLIENT_SECRET:
        data["client_secret"] = settings.MICROSOFT_CLIENT_SECRET

    resp = httpx.post(url_token, data=data, timeout=30.0)
    if resp.status_code == 200:
        j = resp.json()
        refresh_token = j.get("refresh_token")
        print("\n=======================================================")
        print("[OK] REFRESH TOKEN OBTENIDO EXITOSAMENTE:")
        print("=======================================================")
        print(f"MICROSOFT_REFRESH_TOKEN={refresh_token}")
        print("=======================================================")
        print("Copia la línea anterior y pégala en tu archivo .env\n")
    else:
        print(f"ERROR al intercambiar el código ({resp.status_code}): {resp.text}")


if __name__ == "__main__":
    if not settings.MICROSOFT_REFRESH_TOKEN and settings.MICROSOFT_CLIENT_ID:
        generar_refresh_token_interactivo()
    else:
        print("=== Módulo de prueba de API Microsoft Graph / OneDrive ===")
        print(f"ENABLE_CLOUD_API_SYNC: {settings.ENABLE_CLOUD_API_SYNC}")
        print(f"MICROSOFT_CLIENT_ID: {settings.MICROSOFT_CLIENT_ID or 'No configurado'}")
        print(f"ONEDRIVE_REMOTE_FOLDER: {settings.ONEDRIVE_REMOTE_FOLDER}")

        async def _test():
            token = await obtener_token_acceso()
            if token:
                print("[OK] Token obtenido exitosamente!")
            else:
                print("[ERROR] No se pudo obtener el token. Ejecuta el script sin token cargado para iniciar sesión.")

        asyncio.run(_test())
