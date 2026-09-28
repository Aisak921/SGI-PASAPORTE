# SGI-Pasaportes — Fase 1 (versión funcional local)

Mesa de ayuda + inventario TI para la Oficina de Pasaportes. Telegram (long polling) + Groq (Whisper + LLM) + PostgreSQL, todo en Docker, $0/mes.

## Arranque rápido

1. **Bot:** créalo con @BotFather y copia el token.
2. **IDs de Telegram (antes de arrancar el bot):** el bot necesita `SUPPORT_CHAT_ID` para iniciar, así que los IDs se sacan por la API:
   - Escríbele "hola" al bot en privado, crea el grupo de Soporte TI, agrega el bot y en el grupo envía `/hola@tu_bot`.
   - Abre `https://api.telegram.org/bot<TOKEN>/getUpdates` en el navegador: `message.from.id` es tu ID (para `ADMIN_CHAT_IDS`) y `message.chat.id` del grupo (negativo) es `SUPPORT_CHAT_ID`.
3. **Configura:** `cp .env.example .env` (Windows: `copy .env.example .env`) y completa token, IDs, `GROQ_API_KEY`, modelos y SMTP.
4. **Levanta:** `docker compose up -d --build` (crea tablas y las 13 ubicaciones solo).
5. **Inventario:** copia `data/inventario_ejemplo.csv` a `data/inventario.csv`, edítalo y corre
   `docker compose exec app python -m app.seed data/inventario.csv` (se puede repetir sin duplicar).
6. **Usuarios:** como admin, en privado con el bot: `/alta <user_id> <funcionario|soporte|jefatura|admin> <nombre>`.
   También `/desactivar <user_id>` y `/usuarios`. Quien no esté en la lista recibe su ID para pasárselo al admin.

## Flujo

| Quién | Qué hace |
|---|---|
| Funcionario (privado) | Nota de voz o texto → "Reporte recibido, Soporte TI notificado" (se guarda antes de llamar a la IA) |
| Bot → grupo TI | Caso estructurado + audio original + `[✋ Tomar Caso]` (atómico) |
| Técnico asignado | **Reply** (voz o texto) al mensaje del caso → borrador con MTTR → `Confirmar` / `Baja-Reemplazo` / `Grabar de nuevo` |
| Técnico (privado) | "Reporto y cierro atención presencial en Módulo 4…" → registro directo cerrado |
| Baja | Elige el equipo de `Recambio` con botones → afectado queda `Pendiente de Baja`, el recambio pasa a operativo en esa estación, correo HTML a Jefatura |

## Decisiones y supuestos (revísalos)

- **Áreas monodispositivo:** asumí que solo los Módulos 1–7 lo son; el resto exige etiqueta/QR si hay varios equipos del mismo tipo. Ajusta `UBICACIONES_MONODISPOSITIVO` en `app/catalogo.py`.
- **`fechahora_toma`:** Telegram no envía la hora del clic de un botón, así que se usa la hora del servidor (UTC). `reporte` y `cierre` sí usan `message.date`.
- **Hora de cierre:** la fija el *primer* audio de cierre. Si el técnico regraba, cambia el diagnóstico pero no la hora.
- **Registro directo:** inicio = cierre = hora del audio, por eso `mttr_segundos` queda `NULL`. Exclúyelos (`origen='registro_directo'`) en promedios de MTTR.
- **Cierre por voz o texto, siempre en reply** al mensaje del caso, para que conversaciones normales en el grupo no cierren casos por accidente.
- **Recambio por botones**, no por voz: evita seriales mal transcritos.
- Si la IA falla o no identifica el equipo, el caso igual llega a TI (con ⚠️ y el audio); nunca se pierde un reporte.
- Si el SMTP falla, el caso se cierra igual y un job reintenta el correo cada 5 min.

## Probado y no probado

Probado con PostgreSQL 16 real y Telegram/Groq simulados: idempotencia por `update_id`, doble clic en "Tomar", cierre por reply, hora congelada al regrabar, baja con recambio (transacción), registro directo, IA caída, envío SMTP simulado.
**No probado** contra Telegram, Groq ni un SMTP reales: haz una prueba de punta a punta con 1 funcionario y 1 técnico antes de usarlo en producción.

## Backups

`docker compose exec -T db pg_dump -U sgi sgi > backup_$(date +%F).sql` (programa esto con cron; la BD es el registro oficial de activos).

## Pendiente para Fase 2

Historial de movimientos de activos · aprobación de Jefatura para pasar a `dado_de_baja` · detección de duplicados · escalamiento por tiempo sin tomar · recuperación de casos `recibida` tras un reinicio · límite de duración de audio · Alembic para migraciones.

## Reportes en Excel (nube $0 vía OneDrive)

Hay **dos libros independientes**, cada uno sincronizado por su cuenta a una carpeta de OneDrive:

- **`Inventario Oficina Pasaportes - copia.xlsx`**: tu Excel de inventario. El bot reescribe `Inventario Detallado` y `Equipo de Recambio` con los datos actuales de Postgres (fuente de verdad) y agrega la pestaña `Dashboard_Inventario` con KPIs y gráficos. `Buscador Dinámico` y `Configuracion` quedan intactos.
- **`Reporte_Gestion_Soporte_TI.xlsx`**: libro nuevo, generado desde cero cada vez, con `Dashboard_Soporte` y `Bitácora_Detallada` para el período que pidas.

**"Nube" sin costo:** no se usa la API de OneDrive. Los archivos se escriben en una carpeta local que tú sincronizas con el cliente de escritorio de OneDrive (gratis, ya lo tienes en Windows; en Linux usa el cliente libre [abraunegg/onedrive](https://github.com/abraunegg/onedrive)). El propio cliente sube el cambio en cuanto lo detecta — el bot nunca habla con OneDrive directamente.

### Configurar (una sola vez)
1. En tu PC, dentro de la carpeta que ya sincroniza OneDrive, crea una subcarpeta para esto (ej. `SGI-Pasaportes`).
2. Copia ahí tu archivo `Inventario Oficina Pasaportes - copia.xlsx` (el `Reporte_Gestion_Soporte_TI.xlsx` no hace falta copiarlo, se crea solo).
3. En `.env`, define `ONEDRIVE_LOCAL_FOLDER` apuntando a esa carpeta (ej. `C:\Users\tu_usuario\OneDrive\SGI-Pasaportes` en Windows, o la ruta equivalente en Linux).
4. `docker compose up -d --build`.

### Cómo se actualizan
- **Automático:** cada baja/reemplazo de equipo sincroniza el inventario al instante. Además, un job de fondo refresca ambos archivos cada 6 horas aunque nadie los pida.
- **A pedido, por Telegram** (roles admin/soporte/jefatura, en privado con el bot):
  - `/sync_inventario` — actualiza el inventario y te lo manda como archivo.
  - `/informe_soporte [diario|semanal|mensual]` — genera el reporte de soporte TI (por defecto mensual) y te lo manda como archivo.
