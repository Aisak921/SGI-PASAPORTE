"""Punto de entrada: Telegram en long polling (no requiere IP pública ni túneles)."""
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from . import handlers as h
from .config import settings
from .database import init_db
from .seed import seed_base

log = logging.getLogger("sgi")


async def post_init(app: Application) -> None:
    await init_db()
    await seed_base()
    Path(settings.AUDIO_DIR).mkdir(parents=True, exist_ok=True)
    chat = await app.bot.get_chat(settings.SUPPORT_CHAT_ID)  # falla rápido si el bot no está en el grupo
    log.info("Canal de Soporte TI: %s", chat.title)


def main() -> None:
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # no imprimir el token en las URLs

    # concurrent_updates: una llamada lenta a Groq no bloquea los botones "Tomar Caso".
    app = ApplicationBuilder().token(settings.TELEGRAM_BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()

    privado = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("chatid", h.cmd_chatid))  # utilidad de setup (cualquier chat)
    app.add_handler(CommandHandler("start", h.cmd_start, filters=privado))
    app.add_handler(CommandHandler("help", h.cmd_help, filters=privado))
    app.add_handler(CommandHandler("ayuda", h.cmd_help, filters=privado))
    app.add_handler(CommandHandler("alta", h.cmd_alta, filters=privado))
    app.add_handler(CommandHandler("nombre", h.cmd_nombre, filters=privado))
    app.add_handler(CommandHandler("desactivar", h.cmd_desactivar, filters=privado))
    app.add_handler(CommandHandler("usuarios", h.cmd_usuarios, filters=privado))
    app.add_handler(CommandHandler("sync_inventario", h.cmd_sync_inventario, filters=privado))
    app.add_handler(CommandHandler("cargar_inventario", h.cmd_cargar_inventario, filters=privado))
    app.add_handler(CommandHandler("informe_soporte", h.cmd_informe_soporte, filters=privado))
    app.add_handler(CallbackQueryHandler(h.on_callback, pattern=r"^(tomar|cerrar|regrabar|volver|baja|afect|repl):"))
    # Cierre: voz o texto en REPLY al mensaje del caso, dentro del grupo de Soporte TI.
    app.add_handler(
        MessageHandler(
            filters.Chat(settings.SUPPORT_CHAT_ID) & filters.REPLY & (filters.VOICE | filters.TEXT) & ~filters.COMMAND,
            h.on_reply_soporte,
        )
    )
    # Reportes, registros directos y carga de inventario: chat privado con el bot.
    app.add_handler(MessageHandler(privado & (filters.VOICE | filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, h.on_privado))
    app.add_error_handler(h.on_error)

    app.job_queue.run_repeating(h.job_reintentar_correos, interval=300, first=60)
    app.job_queue.run_repeating(h.job_actualizar_reportes, interval=6 * 3600, first=180)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
