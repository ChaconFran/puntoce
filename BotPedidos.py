import logging
import os
import json
import asyncio
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from string import Template
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes
)

# ─── CONFIGURACIÓN ────────────────────────────────────────────────
BOT_TOKEN = "8997518264:AAFPKhQaqZfT83JPN-KOoLiowqq0-yQTjTs"

# ID del grupo de administradores donde llegan los pedidos.
GRUPO_ADMIN_ID = -1004416626509

# Todas las rutas de datos se anclan a la carpeta donde está este script, no a la
# carpeta desde la que lo lances. Así, aunque un día lo arranques desde otro sitio
# (otro terminal, otra sesión de Termux...), siempre lee y escribe el MISMO archivo
# — antes, al usar una ruta relativa, un simple cambio de carpeta hacía que pareciera
# que los cambios "no se guardaban".
# Carpeta donde viven TODOS los archivos de datos (base de datos + stock).
# - Local/Termux: por defecto es la propia carpeta del script (no hace falta tocar nada).
# - Railway (o cualquier hosting con sistema de archivos efímero): PON un Volume
#   montado (ej. en /data) y define la variable de entorno DATA_DIR=/data. Si no lo
#   haces, cada redeploy te borra pedidos.json y los .txt de stock, aunque el código
#   esté perfecto — es lo que te estaba pasando.
CARPETA_BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", CARPETA_BASE)
DB_FILE = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "pedidos.json"))

logging.basicConfig(level=logging.INFO)

# ─── PLATAFORMAS / RESTAURANTES DISPONIBLES ───────────────────────
# clave interna -> (texto a mostrar, precio fijo en €)
# Edita esta lista cuando quieras añadir, quitar o cambiar precios.
PLATAFORMAS = {
    "telepizza":   ("Telepizza",    5.0),
    "papajohns":   ("Papa John's",  5.0),
    "kfc":         ("KFC",          7.0),
}

# Opciones que NO son comida: precio fijo y cada una pide su propia
# información en vez de "qué quieres pedir" (y no piden teléfono/dirección).
# clave -> (nombre, precio fijo en €, pregunta específica a mostrar)
EVENTOS = {
    "cinesa":     ("Cinesa", 5.0, "🎬 Dinos la película, la sesión (fecha y hora) y cuántas entradas quieres."),
    "forvenues":  ("Fever / Venues", 5.0, "🎫 Dinos el evento, la fecha y cuántas entradas quieres."),
}

# Precio fijo para "Pedido personalizado" (ya no se pregunta el importe total).
PRECIO_PERSONALIZADO = 6.0

# ─── PRODUCTOS: CC (Cold Culture) ───────────────────────────────────
# Antes "Ropa". Cada prenda tiene su propio archivo de stock (un ID de
# producto por línea). Al confirmarse el pago, se toma automáticamente
# el siguiente ID disponible y se le envía al comprador; ese ID se borra
# del archivo para no repetirlo.
ROPA = {
    "camisetas":  ("Camisetas", 5.0, os.path.join(DATA_DIR, "stock_camisetas.txt")),
    "pantalones": ("Pantalones", 8.0, os.path.join(DATA_DIR, "stock_pantalones_cc.txt")),
}

def tomar_id_stock(stock_file):
    """Devuelve el siguiente ID disponible del archivo de stock y lo elimina de la lista.
    Devuelve None si el archivo no existe o está vacío."""
    if not os.path.exists(stock_file):
        return None
    with open(stock_file, "r", encoding="utf-8") as f:
        lineas = [l.strip() for l in f if l.strip()]
    if not lineas:
        return None
    id_tomado = lineas[0]
    with open(stock_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lineas[1:]) + ("\n" if len(lineas) > 1 else ""))
    return id_tomado

def contar_stock(stock_file):
    if not os.path.exists(stock_file):
        return 0
    with open(stock_file, "r", encoding="utf-8") as f:
        return len([l for l in f if l.strip()])

def contar_stock_por_codigo(stock_file, codigo):
    """Cuenta cuántas líneas del stock empiezan por esos 6 dígitos (ej. '123456...')."""
    if not os.path.exists(stock_file):
        return 0
    with open(stock_file, "r", encoding="utf-8") as f:
        return sum(1 for l in f if l.strip() and l.strip()[:6] == codigo)

def tomar_id_stock_por_codigo(stock_file, codigo):
    """Como tomar_id_stock, pero elimina y devuelve una línea concreta cuyos primeros
    6 caracteres coinciden con el código (no simplemente la primera del archivo)."""
    if not os.path.exists(stock_file):
        return None
    with open(stock_file, "r", encoding="utf-8") as f:
        lineas = [l.strip() for l in f if l.strip()]
    for i, linea in enumerate(lineas):
        if linea[:6] == codigo:
            id_tomado = linea
            restantes = lineas[:i] + lineas[i + 1:]
            with open(stock_file, "w", encoding="utf-8") as f:
                f.write("\n".join(restantes) + ("\n" if restantes else ""))
            return id_tomado
    return None

# ─── PRODUCTOS: SALDO ──────────────────────────────────────────────
# clave -> (texto a mostrar, precio en € o None si no tiene coste)
SALDO = {
    "saldo_100": ("100€", 100.0),
    "saldo_200": ("200€", 200.0),
    "saldo_0":   ("Sin saldo", None),
}

# ID/username de contacto para soporte y para gestionar el pago.
# Pon aquí tu @usuario de Telegram (sin espacios, con o sin la @).
CONTACTO_ADMIN = "zeeropoint"

# ─── SISTEMA DE PAGO: CRIPTOMONEDAS Y TRANSFERENCIA ────────────────
WALLETS = {
    "ltc": "LZbQrAMznX82Kk7HU3VLy4uPMLT6nGn2Zw",
    "eth": "0x20a6e3Fcf696a8C65822d90917C1d6196a02b01A",
}
NOMBRES_CRYPTO = {"ltc": "Litecoin (LTC)", "eth": "Ethereum (ETH)"}
IDS_COINGECKO = {"ltc": "litecoin", "eth": "ethereum"}

# XP que gana el propio comprador por cada euro gastado en una compra confirmada.
# Bajado de 2 a 0.2: con XP_POR_EURO=2, comprar algo de 5€ ya daba XP suficiente
# para saltar varios niveles de golpe en los primeros tramos de la curva.
XP_POR_EURO = 0.2

def obtener_precio_crypto_eur(moneda_id):
    """Consulta la cotización actual en euros de una cripto vía CoinGecko."""
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={moneda_id}&vs_currencies=eur"
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    return data[moneda_id]["eur"]

# ─── SISTEMA DE REFERIDOS / NIVELES ────────────────────────────────
# Canal que hay que verificar para que un referido cuente.
CANAL_PRINCIPAL = "@zeeropointt"

# Cada referido verificado (que se une al canal) suma esta XP fija al referente.
XP_POR_REFERIDO = 10
NIVEL_MAX = 100

# XP necesaria para pasar del nivel n al n+1 = BASE_XP * n^EXPONENTE
# Calibrado para que llegar al nivel 100 equivalga a ~1.000 referidos verificados
# (subido desde ~200, ya que el premio del nivel 100 es ahora un descuento de por vida).
BASE_XP   = 0.25298
EXPONENTE = 1.5

def xp_para_subir(nivel_actual: int) -> int:
    return max(1, round(BASE_XP * (nivel_actual ** EXPONENTE)))

def _construir_umbrales():
    umbrales = [0]
    acumulado = 0
    for n in range(1, NIVEL_MAX):
        acumulado += xp_para_subir(n)
        umbrales.append(acumulado)
    return umbrales

UMBRALES_NIVEL = _construir_umbrales()

def calcular_nivel(xp_total: int):
    nivel = 1
    for n in range(1, NIVEL_MAX):
        if xp_total >= UMBRALES_NIVEL[n]:
            nivel = n + 1
        else:
            break
    nivel = min(nivel, NIVEL_MAX)
    if nivel >= NIVEL_MAX:
        return nivel, 0, 0, 100
    xp_base_nivel = UMBRALES_NIVEL[nivel - 1]
    xp_siguiente  = xp_para_subir(nivel)
    xp_en_nivel   = xp_total - xp_base_nivel
    pct = min(100, int((xp_en_nivel / xp_siguiente) * 100)) if xp_siguiente else 100
    return nivel, xp_en_nivel, xp_siguiente, pct

def barra_visual(pct: int) -> str:
    pct = max(0, min(100, pct))
    return "🟩" * (pct // 10) + "⬜" * (10 - pct // 10)

# Premios por nivel = descuentos canjeables en Zero Shop.
# Edítalos con /setpremio <nivel> <texto> desde Telegram (no hace falta tocar código).
PREMIOS_POR_NIVEL_DEFECTO = {
    10:  "🎉 5% de descuento en tu próxima compra",
    20:  "🎉 10% de descuento en tu próxima compra",
    30:  "🎉 15% de descuento en tu próxima compra",
    40:  "🎉 17% de descuento en tu próxima compra",
    50:  "🎉 20% de descuento en tu próxima compra",
    60:  "🎉 22% de descuento en tu próxima compra",
    70:  "🎉 25% de descuento en tu próxima compra",
    80:  "🎉 27% de descuento en tu próxima compra",
    90:  "🎉 30% de descuento en tu próxima compra",
    100: "👑 35% de descuento de por vida en Zero Shop",
}

# Pon aquí tu ID de Telegram (y el de quien más gestione premios/pedidos).
# Para saber tu ID, escríbele a @userinfobot.
ADMIN_IDS = {6797650469}

def get_premios(db):
    cfg = db.setdefault("_config", {})
    if "premios" not in cfg:
        cfg["premios"] = {str(n): p for n, p in PREMIOS_POR_NIVEL_DEFECTO.items()}
    return {int(n): p for n, p in cfg["premios"].items()}

def set_premio(db, nivel, texto):
    cfg = db.setdefault("_config", {})
    cfg.setdefault("premios", {})[str(nivel)] = texto

def quitar_premio(db, nivel):
    cfg = db.setdefault("_config", {})
    cfg.setdefault("premios", {}).pop(str(nivel), None)

def get_usuario(db, uid):
    uid = str(uid)
    usuarios = db.setdefault("usuarios", {})
    if uid not in usuarios:
        usuarios[uid] = {
            "xp": 0,
            "referrals": [],
            "referred_by": None,
            "referral_rewarded": False,
            "joined_main": False,
            "username": "",
            "claimed_levels": [],
            "entregado_levels": [],
        }
    u = usuarios[uid]
    u.setdefault("xp", 0)
    u.setdefault("referred_by", None)
    u.setdefault("referral_rewarded", False)
    u.setdefault("claimed_levels", [])
    u.setdefault("entregado_levels", [])
    u.setdefault("joined_main", False)
    return u

# ─── BASE DE DATOS (JSON simple) ──────────────────────────────────
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            db = json.load(f)
    else:
        db = {}
    db.setdefault("pedidos", {})
    db.setdefault("ultimo_numero", 0)
    db.setdefault("usuarios", {})
    db.setdefault("_config", {})
    db.setdefault("compras", {})
    db.setdefault("ultimo_compra_numero", 0)
    db.setdefault("regalos", [])  # lista de regalos independientes, ver /addregalo
    db.setdefault("tickets", {})
    db.setdefault("ultimo_ticket_numero", 0)
    return db

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

# ─── ESTADOS DE LA CONVERSACIÓN ────────────────────────────────────
RESTAURANTE, PEDIDO, NOMBRE, EMAIL, TELEFONO, DIRECCION, COMENTARIOS, PRECIO, CONFIRMAR = range(9)

ESTADOS_PEDIDO = {
    "recibido":     "🆕 Recibido",
    "preparacion":  "🔵 En preparación",
    "camino":       "🚚 En camino",
    "entregado":    "✅ Entregado",
    "cancelado":    "❌ Cancelado",
}

ESTADOS_TICKET = {
    "enviado":      "📨 Enviado",
    "recibido":     "📥 Recibido",
    "investigando": "🔍 Investigando",
    "cerrado":      "✅ Ticket cerrado",
}

# ─── TECLADOS ─────────────────────────────────────────────────────
# Nombres visibles de las pestañas del menú principal. Se pueden cambiar
# desde el propio bot con /setnombre <clave> <texto nuevo>, sin tocar código.
ETIQUETAS_DEFECTO = {
    "perfil":         "🎯 GANA DESCUENTOS",
    "pedidos":        "🍔 Pedidos a domicilio",
    "mas_productos":  "🛒 Más productos",
    "cc":             "🧥 CC",
    "cuentas":        "🎬 Cuentas",
    "saldo":          "💰 Saldo",
    "esim":           "📱 eSIM",
    "regalos":        "🎁 Regalo Semanal",
    "soporte":        "🆘 Soporte",
}

def get_etiquetas(db):
    """Nombres actuales de las pestañas (por defecto + lo que se haya cambiado con /setnombre)."""
    return {**ETIQUETAS_DEFECTO, **db.get("_config", {}).get("etiquetas", {})}

# Textos puramente visuales (cabeceras de cada sección, mensaje de bienvenida...).
# Se pueden cambiar desde el propio bot con /settexto <clave> <texto nuevo>.
# Los que llevan una variable la escriben como $variable (ej. $precio); si el
# admin la borra o la escribe mal, simplemente no se sustituye, nunca rompe el bot.
TEXTOS_DEFECTO = {
    "bienvenida": (
        "🛍️ *¡Bienvenido a Zero Shop!*\n\n"
        "Bot para compras automáticas — por si quieres ser de los primeros en ser atendido.\n\n"
        "Elige una categoría:"
    ),
    "titulo_pedidos":       "🍔 *Pedidos a domicilio*\n\nElige una opción:",
    "titulo_mas_productos": "🛒 *Más productos*\n\nElige una categoría:",
    "titulo_cc":            "🧥 *CC — Cold Culture*\n\nElige una prenda:",
    "titulo_cuentas":       "🎬 *Cuentas*\n\nElige una opción (el precio aparece al elegirla):",
    "titulo_saldo":         "💰 *Saldo*\n\nElige la cantidad:",
    "titulo_perfil":        "🎯 *GANA DESCUENTOS*\n\nElige una opción:",
    "titulo_referidos": (
        "🎯 *Referidos*\n\n"
        "Invita a tus amigos, sube de nivel y desbloquea descuentos en Zero Shop.\n\n"
        "Elige una opción:"
    ),
}

def get_texto(db, clave, **kwargs):
    """Devuelve el texto configurado para esa clave (o el de fábrica), sustituyendo
    las variables $nombre que tenga si se pasan por kwargs. Nunca lanza error aunque
    falten o sobren variables."""
    plantilla = db.get("_config", {}).get("textos", {}).get(clave, TEXTOS_DEFECTO.get(clave, ""))
    if kwargs:
        return Template(plantilla).safe_substitute(**kwargs)
    return plantilla

# ─── IMÁGENES POR SECCIÓN ──────────────────────────────────────────
# Cualquier sección (bienvenida, cada categoría...) puede llevar una imagen
# opcional, además de su texto. Se gestiona con /imagenseccion <clave>,
# /quitarimagenseccion <clave> y /verimagenes.
def get_imagen_seccion(db, clave):
    return db.get("_config", {}).get("imagenes", {}).get(clave)

async def mostrar_seccion(destino, context, clave, texto, teclado, es_callback=True):
    """Muestra el texto (y la imagen, si esa sección tiene una) de cualquier pantalla
    del bot, gestionando sola el cambio entre mensaje de texto y de foto."""
    db = load_db()
    imagen = get_imagen_seccion(db, clave)

    if es_callback:
        q = destino
        chat_id = q.from_user.id
        es_foto_actual = bool(q.message.photo)
        if imagen and not es_foto_actual:
            try:
                await q.message.delete()
            except Exception:
                pass
            await context.bot.send_photo(chat_id=chat_id, photo=imagen, caption=texto, parse_mode="Markdown", reply_markup=teclado)
        elif imagen and es_foto_actual:
            await q.edit_message_caption(caption=texto, parse_mode="Markdown", reply_markup=teclado)
        elif not imagen and es_foto_actual:
            try:
                await q.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=chat_id, text=texto, parse_mode="Markdown", reply_markup=teclado)
        else:
            await q.edit_message_text(texto, parse_mode="Markdown", reply_markup=teclado)
    else:
        message = destino
        if imagen:
            await message.reply_photo(photo=imagen, caption=texto, parse_mode="Markdown", reply_markup=teclado)
        else:
            await message.reply_text(texto, parse_mode="Markdown", reply_markup=teclado)

# Nombres de las "sub-pestañas" dentro de cada categoría (ej. los botones de
# Saldo, las prendas de CC, los restaurantes de Pedidos...). Se identifican por
# "categoria:clave" y se editan con /setsubnombre <categoria> <clave> <texto>.
def get_subetiqueta(db, categoria, clave, default):
    return db.get("_config", {}).get("sub_etiquetas", {}).get(f"{categoria}:{clave}", default)

def set_subetiqueta(db, categoria, clave, texto):
    db.setdefault("_config", {}).setdefault("sub_etiquetas", {})[f"{categoria}:{clave}"] = texto

def reset_subetiqueta(db, categoria, clave):
    db.setdefault("_config", {}).setdefault("sub_etiquetas", {}).pop(f"{categoria}:{clave}", None)

def menu_principal():
    db = load_db()
    et = get_etiquetas(db)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(et["perfil"], callback_data="cat_perfil")],
        [InlineKeyboardButton(et["pedidos"], callback_data="cat_pedidos")],
        [InlineKeyboardButton(et["mas_productos"], callback_data="cat_mas_productos")],
        [InlineKeyboardButton(et["regalos"], callback_data="cat_regalo")],
        [InlineKeyboardButton(et["soporte"], callback_data="cat_soporte")],
    ])

def teclado_mas_productos_menu(db):
    et = get_etiquetas(db)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(et["cc"], callback_data="cat_ropa")],
        [InlineKeyboardButton(et["cuentas"], callback_data="cat_pantalones")],
        [InlineKeyboardButton(et["saldo"], callback_data="cat_saldo")],
        [InlineKeyboardButton(et["esim"], callback_data="cat_target")],
        [InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")],
    ])

def teclado_soporte_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎫 Abrir un ticket (prioridad)", callback_data="ticket_nuevo")],
        [InlineKeyboardButton("💬 Contactar directamente", url=f"https://t.me/{CONTACTO_ADMIN}")],
        [InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")],
    ])

def teclado_perfil_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Estadísticas", callback_data="perfil_estadisticas")],
        [InlineKeyboardButton("🎯 Referidos", callback_data="perfil_referidos")],
        [InlineKeyboardButton("📦 Mis pedidos", callback_data="perfil_pedidos")],
        [InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")],
    ])

def teclado_referidos_submenu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Mi enlace", callback_data="ref_enlace")],
        [InlineKeyboardButton("✅ Verificar suscripción", callback_data="ref_verificar")],
        [InlineKeyboardButton("🏅 Ranking TOP", callback_data="ref_ranking")],
        [InlineKeyboardButton("🔙 Volver", callback_data="cat_perfil")],
    ])

def teclado_pedidos_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Hacer un pedido", callback_data="nuevo_pedido")],
        [InlineKeyboardButton("📦 Ver mi pedido", callback_data="ver_estado")],
        [InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")],
    ])

def teclado_ropa_menu(db):
    filas = [
        [InlineKeyboardButton(get_subetiqueta(db, 'cc', clave, nombre), callback_data=f"ropa_{clave}")]
        for clave, (nombre, precio, _stock) in ROPA.items()
    ]
    filas.append([InlineKeyboardButton("🔙 Volver", callback_data="cat_mas_productos")])
    return InlineKeyboardMarkup(filas)

def teclado_saldo_menu(db):
    filas = [
        [InlineKeyboardButton(get_subetiqueta(db, "saldo", clave, texto_defecto), callback_data=f"{clave}")]
        for clave, (texto_defecto, _precio) in SALDO.items()
    ]
    filas.append([InlineKeyboardButton("🔙 Volver", callback_data="cat_mas_productos")])
    return InlineKeyboardMarkup(filas)

def volver_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")]])

def cancelar_conv_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar pedido", callback_data="cancelar_conv")]])

def teclado_restaurantes(db):
    filas = [
        [InlineKeyboardButton(f"{get_subetiqueta(db, 'pedidos', clave, nombre)} — {precio:.2f}€", callback_data=f"restaurante_{clave}")]
        for clave, (nombre, precio) in PLATAFORMAS.items()
    ]
    filas += [
        [InlineKeyboardButton(f"{get_subetiqueta(db, 'pedidos', clave, nombre)} — {precio:.2f}€", callback_data=f"evento_{clave}")]
        for clave, (nombre, precio, _pregunta) in EVENTOS.items()
    ]
    filas.append([InlineKeyboardButton("✏️ Pedido personalizado", callback_data="restaurante_personalizado")])
    filas.append([InlineKeyboardButton("❌ Cancelar pedido", callback_data="cancelar_conv")])
    return InlineKeyboardMarkup(filas)

def teclado_admin(numero):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔵 En preparación", callback_data=f"estado_{numero}_preparacion"),
         InlineKeyboardButton("🚚 En camino", callback_data=f"estado_{numero}_camino")],
        [InlineKeyboardButton("✅ Entregado", callback_data=f"estado_{numero}_entregado"),
         InlineKeyboardButton("❌ Cancelar", callback_data=f"estado_{numero}_cancelado")]
    ])

# ─── MENÚ PRINCIPAL ────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db = load_db()
    uid = str(user.id)
    u_data = get_usuario(db, uid)
    u_data["username"] = user.username or user.first_name

    bonus_msg = ""
    if context.args and not u_data["referred_by"]:
        referrer_id = context.args[0]
        if referrer_id != uid and referrer_id in db["usuarios"]:
            referrer = get_usuario(db, referrer_id)
            if uid not in referrer["referrals"]:
                referrer["referrals"].append(uid)
            u_data["referred_by"] = referrer_id
            bonus_msg = "\n\n👋 ¡Te han invitado! Ve a 🎯 *GANA DESCUENTOS → Referidos* → únete al canal y pulsa *Verificar suscripción* para que tu amigo reciba su XP."

    save_db(db)
    await mostrar_seccion(update.message, context, "bienvenida", get_texto(db, "bienvenida") + bonus_msg, menu_principal(), es_callback=False)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vuelve al menú principal en cualquier momento (también corta un pedido a medias)."""
    context.user_data.clear()
    db = load_db()
    await mostrar_seccion(update.message, context, "bienvenida", get_texto(db, "bienvenida"), menu_principal(), es_callback=False)
    return ConversationHandler.END

async def volver_al_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    await mostrar_seccion(q, context, "bienvenida", get_texto(db, "bienvenida"), menu_principal())

async def categoria_mas_productos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    await mostrar_seccion(q, context, "titulo_mas_productos", get_texto(db, "titulo_mas_productos"), teclado_mas_productos_menu(db))

async def categoria_soporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🆘 *Soporte*\n\n"
        "¿Tienes un problema con una compra o pedido? Abre un ticket para que se atienda con prioridad.\n"
        "Para cualquier otra consulta, puedes escribirnos directamente.",
        parse_mode="Markdown",
        reply_markup=teclado_soporte_menu()
    )

# ─── SISTEMA DE TICKETS DE SOPORTE ──────────────────────────────────
def teclado_admin_ticket(numero):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Recibido", callback_data=f"ticketestado_{numero}_recibido"),
         InlineKeyboardButton("🔍 Investigando", callback_data=f"ticketestado_{numero}_investigando")],
        [InlineKeyboardButton("✅ Cerrar ticket", callback_data=f"ticketestado_{numero}_cerrado")],
    ])

def texto_para_admin_ticket(numero, t):
    return f"""
🎫 *TICKET #{numero}*

👤 *De:* @{t['username']} (ID `{t['usuario_id']}`)
📦 *Pedido/compra afectada:* {t['motivo']}
📝 *Descripción:* {t['descripcion']}

📌 Estado: {ESTADOS_TICKET[t['estado']]}
"""

async def ticket_nuevo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["ticket_paso"] = "motivo"
    await q.edit_message_text(
        "🎫 *Nuevo ticket*\n\n"
        "¿Qué pedido o compra tiene el problema? (número si lo tienes, o una breve descripción)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")]])
    )

async def capturar_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captura las 2 respuestas del cuestionario de ticket. No hace nada si no se
    estaba en ese flujo (fuera de esto, se ignora el mensaje)."""
    paso = context.user_data.get("ticket_paso")
    if not paso:
        return

    texto = update.message.text.strip()

    if paso == "motivo":
        context.user_data["ticket_motivo"] = texto
        context.user_data["ticket_paso"] = "descripcion"
        await update.message.reply_text(
            "📝 Describe brevemente el problema.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")]])
        )
        return

    if paso == "descripcion":
        user = update.effective_user
        db = load_db()
        db["ultimo_ticket_numero"] += 1
        numero = db["ultimo_ticket_numero"]
        ticket = {
            "usuario_id": user.id,
            "username": user.username or user.first_name,
            "motivo": context.user_data.get("ticket_motivo", "-"),
            "descripcion": texto,
            "estado": "enviado",
            "creado_at": datetime.now().isoformat(),
        }
        db["tickets"][str(numero)] = ticket
        save_db(db)
        context.user_data.pop("ticket_paso", None)
        context.user_data.pop("ticket_motivo", None)

        await update.message.reply_text(
            f"✅ *Ticket #{numero} enviado.*\n\nEstado: {ESTADOS_TICKET['enviado']}\n"
            f"Te avisaremos aquí mismo según lo vayamos gestionando.",
            parse_mode="Markdown",
            reply_markup=volver_menu_keyboard()
        )

        try:
            await context.bot.send_message(
                chat_id=GRUPO_ADMIN_ID,
                text=texto_para_admin_ticket(numero, ticket),
                parse_mode="Markdown",
                reply_markup=teclado_admin_ticket(numero)
            )
        except Exception as e:
            logging.error(f"No se pudo enviar el ticket #{numero} al grupo de admins: {e}")

async def ticketestado_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, numero_str, nuevo_estado = q.data.split("_")
    db = load_db()
    ticket = db["tickets"].get(numero_str)
    if not ticket:
        await q.answer("Ticket no encontrado.", show_alert=True)
        return

    ticket["estado"] = nuevo_estado
    save_db(db)

    await q.edit_message_text(
        texto_para_admin_ticket(numero_str, ticket),
        parse_mode="Markdown",
        reply_markup=teclado_admin_ticket(numero_str)
    )

    try:
        await context.bot.send_message(
            chat_id=ticket["usuario_id"],
            text=f"🎫 Tu ticket *#{numero_str}* ahora está: {ESTADOS_TICKET[nuevo_estado]}",
            parse_mode="Markdown"
        )
    except Exception:
        pass

async def cmd_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista los tickets abiertos (no cerrados). Uso interno del equipo, como /cola."""
    if not es_admin(update.effective_user.id):
        return
    db = load_db()
    activos = [(int(n), t) for n, t in db["tickets"].items() if t["estado"] != "cerrado"]
    if not activos:
        await update.message.reply_text("✅ No hay tickets abiertos.")
        return
    activos.sort()
    lineas = [f"#{n} — @{t['username']} — {ESTADOS_TICKET[t['estado']]}" for n, t in activos]
    await update.message.reply_text("🎫 *Tickets abiertos:*\n\n" + "\n".join(lineas), parse_mode="Markdown")

# ─── CATEGORÍA: PERFIL (Estadísticas / Referidos / Mis pedidos) ────
async def categoria_perfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    await mostrar_seccion(q, context, "titulo_perfil", get_texto(db, "titulo_perfil"), teclado_perfil_menu())

async def perfil_referidos_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    await mostrar_seccion(q, context, "titulo_referidos", get_texto(db, "titulo_referidos"), teclado_referidos_submenu())

async def notificar_nuevos_premios(context, uid, u, nivel_anterior, nivel_nuevo, premios):
    nuevos_niveles = [n for n in premios if nivel_anterior < n <= nivel_nuevo]
    for n in sorted(nuevos_niveles):
        if n in u["claimed_levels"]:
            continue
        u["claimed_levels"].append(n)
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"🎉 *¡NIVEL {n} ALCANZADO!*\n\nDesbloqueaste:\n{premios[n]}\n\nMenciónalo cuando hables con nosotros para el pago.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

# Máximo de niveles que un solo evento (un referido, una compra) puede hacer subir de golpe.
# Evita que una compra grande o incluso el primer referido dispare al usuario muchos niveles
# de una vez, ya que los primeros niveles de la curva cuestan muy poca XP.
MAX_NIVELES_POR_EVENTO = 1

def agregar_xp_con_tope(xp_actual, xp_ganada_bruta):
    """Suma XP a un total, pero sin permitir que un único evento cruce más de
    MAX_NIVELES_POR_EVENTO niveles. El excedente de XP no se concede (se pierde),
    es la forma más simple y fiable de que "una compra" o "un referido" no dispare
    de golpe media tabla de niveles."""
    nivel_antes, *_ = calcular_nivel(xp_actual)
    xp_propuesta = xp_actual + xp_ganada_bruta
    nivel_propuesto, *_ = calcular_nivel(xp_propuesta)
    nivel_tope = min(NIVEL_MAX, nivel_antes + MAX_NIVELES_POR_EVENTO)

    if nivel_propuesto <= nivel_tope:
        return xp_propuesta

    if nivel_tope >= NIVEL_MAX:
        return xp_propuesta  # ya en el tope de niveles disponible, no hay más que limitar

    xp_maxima_en_tope = UMBRALES_NIVEL[nivel_tope] - 1  # justo antes de cruzar al siguiente nivel
    return max(xp_actual, min(xp_propuesta, xp_maxima_en_tope))

async def otorgar_xp(context, referrer_id, referrer_data, premios):
    nivel_antes, *_ = calcular_nivel(referrer_data["xp"])
    referrer_data["xp"] = agregar_xp_con_tope(referrer_data["xp"], XP_POR_REFERIDO)
    nivel_despues, _, _, _ = calcular_nivel(referrer_data["xp"])

    if nivel_despues > nivel_antes:
        try:
            await context.bot.send_message(
                chat_id=int(referrer_id),
                text=f"⬆️ *¡SUBISTE DE NIVEL!*\n\nAhora eres *nivel {nivel_despues}* (+{XP_POR_REFERIDO} XP por un nuevo referido verificado).",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    await notificar_nuevos_premios(context, referrer_id, referrer_data, nivel_antes, nivel_despues, premios)

async def otorgar_xp_compra(context, uid, u, precio_eur, premios):
    """El propio comprador gana XP por su compra confirmada (según lo gastado)."""
    xp_ganada = round(precio_eur * XP_POR_EURO)
    nivel_antes, *_ = calcular_nivel(u["xp"])
    u["xp"] = agregar_xp_con_tope(u["xp"], xp_ganada)
    nivel_despues, _, _, _ = calcular_nivel(u["xp"])

    try:
        await context.bot.send_message(
            chat_id=int(uid),
            text=f"⭐ Has ganado *{xp_ganada} XP* por tu compra.",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    if nivel_despues > nivel_antes:
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"⬆️ *¡SUBISTE DE NIVEL!*\n\nAhora eres *nivel {nivel_despues}*.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    await notificar_nuevos_premios(context, uid, u, nivel_antes, nivel_despues, premios)

# ─── SISTEMA DE COMPRAS Y PAGO (cripto / transferencia) ────────────
def crear_compra(db, comprador_id, username, producto, precio_eur, auto_entrega=False, stock_file=None, stock_codigo=None):
    db["ultimo_compra_numero"] += 1
    compra_id = db["ultimo_compra_numero"]
    db["compras"][str(compra_id)] = {
        "comprador_id": comprador_id,
        "username": username,
        "producto": producto,
        "precio_eur": precio_eur,
        "metodo": None,
        "estado": "pendiente_metodo",
        "hash": None,
        "creado_at": datetime.now().isoformat(),
        "auto_entrega": auto_entrega,
        "stock_file": stock_file,
        "stock_codigo": stock_codigo,
    }
    save_db(db)
    return compra_id

def teclado_metodo_pago(compra_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🪙 Criptomonedas", callback_data=f"pagocrypto_{compra_id}")],
        [InlineKeyboardButton("🏦 Transferencia instantánea", callback_data=f"pagotransfer_{compra_id}")],
        [InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")],
    ])

async def iniciar_pago(query, context, compra_id, precio_eur, producto):
    texto = f"💳 *Pago — {producto}*\n\nTotal a pagar: *{precio_eur:.2f}€*\n\nElige el método de pago:"
    await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=teclado_metodo_pago(compra_id))

async def pago_cripto_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    compra_id = q.data.split("_", 1)[1]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Ł Litecoin (LTC)", callback_data=f"pagomoneda_{compra_id}_ltc")],
        [InlineKeyboardButton("Ξ Ethereum (ETH)", callback_data=f"pagomoneda_{compra_id}_eth")],
        [InlineKeyboardButton("🔙 Volver", callback_data=f"volverpago_{compra_id}")],
    ])
    await q.edit_message_text("🪙 *Elige la criptomoneda:*", parse_mode="Markdown", reply_markup=kb)

async def volver_metodo_pago_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    compra_id = q.data.split("_", 1)[1]
    db = load_db()
    compra = db["compras"].get(compra_id)
    if not compra:
        await q.edit_message_text("❌ No se encontró esta compra.")
        return
    await iniciar_pago(q, context, compra_id, compra["precio_eur"], compra["producto"])

async def pago_moneda_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, compra_id, moneda = q.data.split("_")
    db = load_db()
    compra = db["compras"].get(compra_id)
    if not compra:
        await q.edit_message_text("❌ No se encontró esta compra.")
        return

    try:
        precio_unidad = await asyncio.get_event_loop().run_in_executor(
            None, obtener_precio_crypto_eur, IDS_COINGECKO[moneda]
        )
    except Exception:
        await q.edit_message_text(
            "⚠️ No se pudo obtener la cotización en tiempo real. Inténtalo de nuevo en unos segundos.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data=f"pagocrypto_{compra_id}")]])
        )
        return

    cantidad = compra["precio_eur"] / precio_unidad
    compra["metodo"] = moneda
    compra["estado"] = "esperando_hash"
    compra["cantidad_crypto"] = cantidad
    compra["precio_unidad_en_pago"] = precio_unidad
    save_db(db)

    context.user_data["compra_esperando_hash"] = compra_id

    texto = f"""
🪙 *Pago con {NOMBRES_CRYPTO[moneda]}*

Importe: *{compra['precio_eur']:.2f}€* ≈ *{cantidad:.8f} {moneda.upper()}*
(cotización en tiempo real: 1 {moneda.upper()} = {precio_unidad:.2f}€)

📥 Envía esa cantidad exacta a esta dirección:
`{WALLETS[moneda]}`

⚠️ *Una vez realizado el pago, envía aquí mismo el HASH de la transacción de la blockchain para completar el pedido.*
"""
    await q.edit_message_text(texto, parse_mode="Markdown")

async def pago_transferencia_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    compra_id = q.data.split("_", 1)[1]
    db = load_db()
    compra = db["compras"].get(compra_id)
    if not compra:
        await q.edit_message_text("❌ No se encontró esta compra.")
        return

    compra["metodo"] = "transferencia"
    compra["estado"] = "pendiente_manual"
    save_db(db)

    await q.edit_message_text(
        f"🏦 *Transferencia instantánea*\n\n"
        f"Importe: *{compra['precio_eur']:.2f}€*\n\n"
        f"Contacta con nosotros para realizar el pago manualmente.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contactar para pagar", url=f"https://t.me/{CONTACTO_ADMIN}")]])
    )

    try:
        await context.bot.send_message(
            chat_id=GRUPO_ADMIN_ID,
            text=(
                f"🏦 *TRANSFERENCIA PENDIENTE — Compra #{compra_id}*\n\n"
                f"Producto: {compra['producto']}\n"
                f"Importe: {compra['precio_eur']:.2f}€\n"
                f"Comprador: @{compra['username']} (ID `{compra['comprador_id']}`)"
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirmar pago recibido", callback_data=f"compraconfirmar_{compra_id}")]])
        )
    except Exception as e:
        logging.error(f"No se pudo notificar la compra #{compra_id} al grupo: {e}")

# ─── VERIFICACIÓN AUTOMÁTICA DEL HASH EN LA BLOCKCHAIN ────────────
# Usa BlockCypher (API pública, sin necesidad de clave para este volumen).
DECIMALES_CRYPTO = {"ltc": 1e8, "eth": 1e18}
RED_BLOCKCYPHER = {"ltc": "ltc/main", "eth": "eth/main"}

MIN_CONFIRMACIONES = 1          # confirmaciones mínimas en la blockchain para aceptar el pago
TOLERANCIA_IMPORTE = 0.03       # 3% de margen por si la cotización se movió entre mostrarla y pagar

def verificar_tx_blockchain(moneda, tx_hash):
    """Consulta BlockCypher. Devuelve (encontrada, confirmaciones, importe_recibido_en_wallet)."""
    red = RED_BLOCKCYPHER[moneda]
    url = f"https://api.blockcypher.com/v1/{red}/txs/{tx_hash}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, 0, 0.0
        raise

    confirmaciones = data.get("confirmations", 0)
    direccion = WALLETS[moneda].lower()
    total_unidades_base = 0
    for salida in data.get("outputs", []):
        direcciones_salida = [a.lower() for a in salida.get("addresses", [])]
        if direccion in direcciones_salida:
            total_unidades_base += salida.get("value", 0)

    importe_recibido = total_unidades_base / DECIMALES_CRYPTO[moneda]
    return True, confirmaciones, importe_recibido

async def _confirmar_compra(context, db, compra_id, compra):
    """Marca la compra como confirmada, otorga XP y hace la entrega automática si aplica.
    Usada tanto por la verificación automática como por el botón manual del admin.
    Devuelve (texto_extra_para_comprador, texto_extra_para_admin)."""
    if compra.get("estado") == "confirmada":
        # Ya se confirmó por la otra vía (cerrojo de seguridad extra) — no repetir XP ni stock.
        return "", ""
    compra["estado"] = "confirmada"
    comprador_id = str(compra["comprador_id"])
    comprador = get_usuario(db, comprador_id)
    await otorgar_xp_compra(context, comprador_id, comprador, compra["precio_eur"], get_premios(db))

    extra_comprador = ""
    extra_admin = ""
    if compra.get("auto_entrega"):
        if compra.get("stock_codigo"):
            id_producto = tomar_id_stock_por_codigo(compra["stock_file"], compra["stock_codigo"])
        else:
            id_producto = tomar_id_stock(compra["stock_file"])
        if id_producto:
            extra_comprador = f"\n\n🎟️ *Tu ID de producto:* `{id_producto}`"
        else:
            extra_comprador = "\n\n⚠️ Sin stock disponible ahora mismo. Te lo enviaremos en cuanto repongamos, disculpa la espera."
            extra_admin = f"\n\n⚠️ *SIN STOCK* para reponer la compra #{compra_id} ({compra['producto']}). Revisa `{compra['stock_file']}`."

    save_db(db)
    return extra_comprador, extra_admin

async def capturar_hash_pago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captura el siguiente mensaje de texto del usuario como el hash de la transacción,
    solo si estaba pendiente de enviarlo (fuera de esto, no hace nada). Intenta verificarlo
    automáticamente contra la blockchain; si no puede, cae al mismo flujo manual de siempre."""
    compra_id = context.user_data.get("compra_esperando_hash")
    if not compra_id:
        return

    hash_tx = update.message.text.strip()
    db = load_db()
    compra = db["compras"].get(compra_id)
    if not compra:
        context.user_data.pop("compra_esperando_hash", None)
        return

    # Cerrojo anti-doble-ejecución: si esta compra ya no está en "esperando_hash"
    # (porque ya se está verificando, ya se confirmó, o ya cayó a manual), no se
    # vuelve a procesar. Esto es lo que evita que la verificación automática y la
    # confirmación manual del admin puedan "chocar" y ejecutarse las dos para la
    # misma compra (doble XP, doble entrega de stock, etc.).
    if compra.get("estado") != "esperando_hash":
        context.user_data.pop("compra_esperando_hash", None)
        await update.message.reply_text(
            "Esta compra ya está siendo procesada o ya fue verificada, no hace falta reenviar el hash.",
            reply_markup=volver_menu_keyboard()
        )
        return

    compra["estado"] = "verificando"
    save_db(db)

    compra["hash"] = hash_tx
    context.user_data.pop("compra_esperando_hash", None)
    moneda = compra["metodo"]

    await update.message.reply_text("🔍 Verificando el pago en la blockchain, un momento...")

    verificado = False
    motivo_fallo = ""
    try:
        encontrada, confirmaciones, importe_recibido = await asyncio.get_event_loop().run_in_executor(
            None, verificar_tx_blockchain, moneda, hash_tx
        )
        cantidad_esperada = compra.get("cantidad_crypto", 0)
        if not encontrada:
            motivo_fallo = "No se encontró esa transacción en la blockchain."
        elif confirmaciones < MIN_CONFIRMACIONES:
            motivo_fallo = f"Solo tiene {confirmaciones} confirmación(es) (se requieren {MIN_CONFIRMACIONES})."
        elif importe_recibido < cantidad_esperada * (1 - TOLERANCIA_IMPORTE):
            motivo_fallo = f"El importe recibido ({importe_recibido:.8f} {moneda.upper()}) es menor al esperado ({cantidad_esperada:.8f})."
        else:
            verificado = True
    except Exception as e:
        motivo_fallo = f"Error al consultar la blockchain: {e}"

    if verificado:
        extra_comprador, extra_admin = await _confirmar_compra(context, db, compra_id, compra)
        await update.message.reply_text(
            f"✅ *Pago verificado automáticamente.* ¡Gracias!{extra_comprador}",
            parse_mode="Markdown",
            reply_markup=volver_menu_keyboard()
        )
        try:
            await context.bot.send_message(
                chat_id=GRUPO_ADMIN_ID,
                text=(
                    f"✅ *PAGO VERIFICADO AUTOMÁTICAMENTE — Compra #{compra_id}*\n\n"
                    f"Producto: {compra['producto']}\n"
                    f"Importe: {compra['precio_eur']:.2f}€ ≈ {compra.get('cantidad_crypto', 0):.8f} {moneda.upper()}\n"
                    f"Comprador: @{compra['username']} (ID `{compra['comprador_id']}`)\n"
                    f"Hash: `{hash_tx}`" + extra_admin
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"No se pudo notificar la compra #{compra_id} al grupo: {e}")
        return

    # No se pudo verificar sola: cae al flujo manual de siempre
    compra["estado"] = "pendiente_verificacion"
    save_db(db)
    await update.message.reply_text(
        "⏳ No hemos podido verificarlo automáticamente todavía (puede tardar unos minutos en "
        "tener confirmaciones). Lo revisaremos manualmente en breve.",
        reply_markup=volver_menu_keyboard()
    )
    try:
        await context.bot.send_message(
            chat_id=GRUPO_ADMIN_ID,
            text=(
                f"💰 *PAGO PENDIENTE DE VERIFICAR — Compra #{compra_id}*\n\n"
                f"Producto: {compra['producto']}\n"
                f"Importe: {compra['precio_eur']:.2f}€ ≈ {compra.get('cantidad_crypto', 0):.8f} {moneda.upper()}\n"
                f"Comprador: @{compra['username']} (ID `{compra['comprador_id']}`)\n"
                f"Hash: `{hash_tx}`\n"
                f"⚠️ Verificación automática: {motivo_fallo}"
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirmar pago recibido", callback_data=f"compraconfirmar_{compra_id}")]])
        )
    except Exception as e:
        logging.error(f"No se pudo notificar la compra #{compra_id} al grupo: {e}")

async def compra_confirmar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """El admin confirma manualmente en el grupo que el pago ha llegado
    (red de seguridad para cuando la verificación automática no pudo confirmarlo)."""
    q = update.callback_query
    await q.answer()
    compra_id = q.data.split("_", 1)[1]
    db = load_db()
    compra = db["compras"].get(compra_id)
    if not compra:
        await q.answer("Compra no encontrada.", show_alert=True)
        return
    if compra["estado"] == "confirmada":
        await q.answer("Ya estaba confirmada.", show_alert=True)
        return

    extra_comprador, extra_admin = await _confirmar_compra(context, db, compra_id, compra)

    await q.edit_message_text(q.message.text_markdown + "\n\n✅ *PAGO CONFIRMADO*" + extra_admin, parse_mode="Markdown")

    try:
        await context.bot.send_message(
            chat_id=compra["comprador_id"],
            text=f"✅ Hemos confirmado tu pago de la compra *#{compra_id}* ({compra['producto']}). ¡Gracias!" + extra_comprador,
            parse_mode="Markdown"
        )
    except Exception:
        pass

async def perfil_estadisticas_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    uid = str(q.from_user.id)
    u = get_usuario(db, uid)

    nivel, xp_en_nivel, xp_necesaria, pct = calcular_nivel(u["xp"])
    refs = len(u["referrals"])
    bar = barra_visual(pct)
    premios = get_premios(db)

    if nivel >= NIVEL_MAX:
        progreso_txt = "🏆 ¡Nivel máximo alcanzado!"
    else:
        progreso_txt = f"🎯 {xp_en_nivel}/{xp_necesaria} XP para el nivel {nivel + 1}"

    premios_txt = "\n".join(
        f"{'✅' if nivel >= n else '⬜'} Nivel {n} — {p}"
        for n, p in sorted(premios.items())
    ) if premios else "Aún no hay premios configurados."

    texto = f"""
📊 *TUS ESTADÍSTICAS*

👤 Usuario: @{u.get('username','?')}
🏅 Nivel: *{nivel} / {NIVEL_MAX}*
⭐ XP total: *{u['xp']}*
👥 Referidos verificados: *{refs}*

{bar} {pct}%
{progreso_txt}

🏁 *Premios:*
{premios_txt}
"""
    await q.edit_message_text(
        texto, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="cat_perfil")]])
    )

async def perfil_pedidos_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    uid = q.from_user.id

    ESTADOS_COMPRA = {
        "pendiente_metodo": "🕓 Pendiente de elegir método de pago",
        "esperando_hash": "🕓 Esperando el pago",
        "pendiente_verificacion": "🔍 Verificando el pago",
        "pendiente_manual": "🏦 Pendiente de transferencia",
        "confirmada": "✅ Confirmada",
    }

    lineas = []
    pedidos_usuario = sorted(
        ((int(n), p) for n, p in db["pedidos"].items() if p.get("usuario_id") == uid),
        key=lambda x: x[0]
    )
    for numero, p in pedidos_usuario:
        lineas.append(f"🍔 Pedido #{numero} — {p.get('restaurante_nombre', '?')} — {ESTADOS_PEDIDO.get(p['estado'], p['estado'])}")

    compras_usuario = sorted(
        ((int(n), c) for n, c in db["compras"].items() if c.get("comprador_id") == uid),
        key=lambda x: x[0]
    )
    for numero, c in compras_usuario:
        estado_txt = ESTADOS_COMPRA.get(c["estado"], c["estado"])
        lineas.append(f"🛍️ Compra #{numero} — {c['producto']} ({c['precio_eur']:.2f}€) — {estado_txt}")

    tickets_usuario = sorted(
        ((int(n), t) for n, t in db["tickets"].items() if t.get("usuario_id") == uid),
        key=lambda x: x[0]
    )
    for numero, t in tickets_usuario:
        lineas.append(f"🎫 Ticket #{numero} — {ESTADOS_TICKET[t['estado']]}")

    texto = "📦 *TUS PEDIDOS Y COMPRAS*\n\n" + "\n".join(lineas) if lineas else "📦 Aún no tienes ningún pedido ni compra."
    await q.edit_message_text(
        texto, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="cat_perfil")]])
    )

async def ref_enlace_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = str(q.from_user.id)
    bot_user = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_user.username}?start={uid}"
    texto = f"""
🔗 *TU ENLACE ÚNICO*

Compártelo en grupos, canales y con amigos 👇

`{ref_link}`

📌 *Recuerda:*
• Cada persona que use tu enlace y verifique que se unió al canal = *+{XP_POR_REFERIDO} XP*
• Sube de nivel y desbloquea descuentos en Zero Shop 🔥
"""
    await q.edit_message_text(
        texto, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="perfil_referidos")]])
    )

async def ref_verificar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    uid = str(q.from_user.id)
    u = get_usuario(db, uid)

    try:
        m1 = await context.bot.get_chat_member(CANAL_PRINCIPAL, q.from_user.id)
        in_main = m1.status in ("member", "administrator", "creator")
    except Exception:
        in_main = False

    u["joined_main"] = in_main

    recompensa_msg = ""
    if in_main and u.get("referred_by") and not u.get("referral_rewarded"):
        referrer_id = u["referred_by"]
        if referrer_id in db["usuarios"]:
            referrer = get_usuario(db, referrer_id)
            await otorgar_xp(context, referrer_id, referrer, get_premios(db))
            u["referral_rewarded"] = True
            recompensa_msg = "\n\n🎉 ¡Tu amigo que te invitó acaba de recibir su XP!"

    save_db(db)
    s1 = "✅" if in_main else "❌"
    texto = f"""
✅ *VERIFICACIÓN DE CANAL*

{s1} Canal principal

{"🔥 ¡Todo listo!" if in_main else "⚡ Únete al canal para verificarte."}{recompensa_msg}
"""
    await q.edit_message_text(
        texto, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="perfil_referidos")]])
    )

async def ref_ranking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    sorted_users = sorted(db["usuarios"].items(), key=lambda x: x[1].get("xp", 0), reverse=True)[:10]
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = []
    for i, (_, data) in enumerate(sorted_users):
        name = data.get("username", "Anónimo")
        nivel_i, *_ = calcular_nivel(data.get("xp", 0))
        lines.append(f"{medals[i]} *@{name}* — Nivel {nivel_i} ({data.get('xp', 0)} XP)")
    texto = "🏆 *TOP 10 PARTICIPANTES*\n\n" + "\n".join(lines) if lines else "🏆 Aún no hay participantes."
    await q.edit_message_text(
        texto, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="perfil_referidos")]])
    )

# ─── COMANDOS DE ADMINISTRACIÓN (gestión de premios y entregas) ───
def es_admin(user_id, db=None) -> bool:
    if user_id in ADMIN_IDS:
        return True
    if db is None:
        db = load_db()
    return user_id in db.get("_config", {}).get("admin_extra", [])

async def cmd_setpremio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /setpremio <nivel> <texto del premio>")
        return
    try:
        nivel = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El nivel debe ser un número.")
        return
    texto = " ".join(context.args[1:])
    db = load_db()
    set_premio(db, nivel, texto)
    save_db(db)
    await update.message.reply_text(f"✅ Premio del nivel {nivel} guardado:\n{texto}")

async def cmd_quitarpremio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /quitarpremio <nivel>")
        return
    try:
        nivel = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El nivel debe ser un número.")
        return
    db = load_db()
    quitar_premio(db, nivel)
    save_db(db)
    await update.message.reply_text(f"🗑️ Premio del nivel {nivel} eliminado (si existía).")

async def cmd_premios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    db = load_db()
    premios = get_premios(db)
    if not premios:
        await update.message.reply_text("No hay premios configurados todavía.")
        return
    lineas = "\n".join(f"Nivel {n} → {p}" for n, p in sorted(premios.items()))
    await update.message.reply_text(f"🏁 *Premios configurados:*\n\n{lineas}", parse_mode="Markdown")

async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    db = load_db()
    lineas = []
    for uid, data in db["usuarios"].items():
        pendientes = [n for n in data.get("claimed_levels", []) if n not in data.get("entregado_levels", [])]
        if pendientes:
            nombre = data.get("username", "Anónimo")
            niveles = ", ".join(str(n) for n in sorted(pendientes))
            lineas.append(f"@{nombre} (ID `{uid}`) → nivel(es) {niveles}")
    if not lineas:
        await update.message.reply_text("✅ No hay premios pendientes de entregar.")
        return
    texto = "📦 *Premios pendientes de entregar:*\n\n" + "\n".join(lineas)
    texto += "\n\nPara marcarlo: /entregar <ID> <nivel>"
    await update.message.reply_text(texto, parse_mode="Markdown")

async def cmd_entregar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Uso: /entregar <ID_usuario> <nivel>")
        return
    uid, nivel_str = context.args
    try:
        nivel = int(nivel_str)
    except ValueError:
        await update.message.reply_text("El nivel debe ser un número.")
        return
    db = load_db()
    if uid not in db["usuarios"]:
        await update.message.reply_text("No encuentro a ese usuario en la base de datos.")
        return
    data = get_usuario(db, uid)
    if nivel not in data.get("claimed_levels", []):
        await update.message.reply_text("Ese usuario todavía no ha desbloqueado ese nivel.")
        return
    if nivel in data.get("entregado_levels", []):
        await update.message.reply_text("Ya estaba marcado como entregado.")
        return
    data["entregado_levels"].append(nivel)
    save_db(db)
    await update.message.reply_text(f"✅ Marcado como entregado: nivel {nivel} para el usuario {uid}.")

# ─── COMANDOS DE ADMINISTRACIÓN: REGALOS ALEATORIOS ────────────────
async def cmd_addregalo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Crea un nuevo regalo independiente (empieza con 1 unidad).
    Uso: /addregalo <texto>  (admite varias líneas)"""
    if not es_admin(update.effective_user.id):
        return
    partes = update.message.text.split(" ", 1)
    if len(partes) < 2 or not partes[1].strip():
        await update.message.reply_text("Uso: /addregalo <texto>\nCrea un regalo nuevo con 1 unidad disponible.")
        return
    db = load_db()
    cfg = db.setdefault("_config", {})
    cfg["ultimo_regalo_id"] = cfg.get("ultimo_regalo_id", 0) + 1
    nuevo_id = cfg["ultimo_regalo_id"]
    db.setdefault("regalos", []).append({
        "id": nuevo_id,
        "texto": partes[1],
        "imagen_file_id": None,
        "cupo_total": 1,
        "cupo_restante": 1,
        "reclamado_por": [],
    })
    save_db(db)
    await update.message.reply_text(
        f"✅ Regalo #{nuevo_id} creado con 1 unidad.\n"
        f"• /imagenregalo {nuevo_id} — añadirle una foto\n"
        f"• /setcupogregalo {nuevo_id} <n> — cambiar las unidades"
    )

async def cmd_quitarregalo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Elimina un regalo por completo. Uso: /quitarregalo <id>"""
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /quitarregalo <id>")
        return
    try:
        id_ = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El id debe ser un número.")
        return
    db = load_db()
    regalos = db.get("regalos", [])
    nuevos = [g for g in regalos if g["id"] != id_]
    if len(nuevos) == len(regalos):
        await update.message.reply_text("No encuentro ningún regalo con ese id.")
        return
    db["regalos"] = nuevos
    save_db(db)
    await update.message.reply_text(f"🗑️ Regalo #{id_} eliminado.")

async def cmd_setcupogregalo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fija las unidades de un regalo concreto y reinicia quién lo ha reclamado.
    Uso: /setcupogregalo <id> <n>"""
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Uso: /setcupogregalo <id> <n>")
        return
    try:
        id_, n = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.message.reply_text("El id y el cupo deben ser números.")
        return
    db = load_db()
    regalo = next((g for g in db.get("regalos", []) if g["id"] == id_), None)
    if not regalo:
        await update.message.reply_text("No encuentro ningún regalo con ese id.")
        return
    regalo["cupo_total"] = n
    regalo["cupo_restante"] = n
    regalo["reclamado_por"] = []
    save_db(db)
    await update.message.reply_text(f"✅ Regalo #{id_} ahora tiene {n} unidad(es) (reclamos reiniciados).")

async def cmd_imagenregalo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prepara el bot para recibir una foto y asignarla a un regalo concreto.
    Uso: /imagenregalo <id>  y luego mandas la foto."""
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /imagenregalo <id>\nLuego mándame la foto.")
        return
    try:
        id_ = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El id debe ser un número.")
        return
    db = load_db()
    regalo = next((g for g in db.get("regalos", []) if g["id"] == id_), None)
    if not regalo:
        await update.message.reply_text("No encuentro ningún regalo con ese id.")
        return
    cfg = db.setdefault("_config", {})
    cfg["esperando_imagen_regalo_id"] = id_
    save_db(db)
    await update.message.reply_text(f"📷 Envíame ahora la imagen para el regalo #{id_}.")

async def cmd_verregalos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    db = load_db()
    regalos = db.get("regalos", [])
    if not regalos:
        await update.message.reply_text("No hay ningún regalo creado. Usa /addregalo <texto>.")
        return
    lineas = []
    for g in regalos:
        resumen = g["texto"][:40] + ("..." if len(g["texto"]) > 40 else "")
        imagen_txt = "📷" if g.get("imagen_file_id") else "sin imagen"
        lineas.append(f"#{g['id']}: {resumen} — {g['cupo_restante']}/{g['cupo_total']} — {imagen_txt}")
    await update.message.reply_text("🎁 Regalos configurados:\n\n" + "\n".join(lineas))

# ─── COMANDOS DE ADMINISTRACIÓN: IMÁGENES POR SECCIÓN ──────────────
# Claves válidas: las mismas que /textos (bienvenida, titulo_pedidos, titulo_cc,
# titulo_cuentas, titulo_saldo, titulo_perfil, titulo_referidos, titulo_mas_productos)
async def cmd_imagenseccion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prepara el bot para recibir una foto y asignarla a una sección concreta.
    Uso: /imagenseccion <clave>  y luego mandas la foto."""
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text(
            "Uso: /imagenseccion <clave>\nLuego mándame la foto.\n"
            "Claves disponibles: " + ", ".join(TEXTOS_DEFECTO.keys())
        )
        return
    clave = context.args[0]
    if clave not in TEXTOS_DEFECTO:
        await update.message.reply_text("Esa clave no existe. Claves disponibles: " + ", ".join(TEXTOS_DEFECTO.keys()))
        return
    db = load_db()
    db.setdefault("_config", {})["esperando_imagen_seccion"] = clave
    save_db(db)
    await update.message.reply_text(f"📷 Envíame ahora la imagen para la sección «{clave}».")

async def cmd_quitarimagenseccion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /quitarimagenseccion <clave>")
        return
    clave = context.args[0]
    db = load_db()
    db.setdefault("_config", {}).setdefault("imagenes", {}).pop(clave, None)
    save_db(db)
    await update.message.reply_text(f"🗑️ Imagen de la sección «{clave}» eliminada.")

async def cmd_verimagenes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    db = load_db()
    imagenes = db.get("_config", {}).get("imagenes", {})
    lineas = [f"{clave} → {'📷 con imagen' if clave in imagenes else 'sin imagen'}" for clave in TEXTOS_DEFECTO]
    await update.message.reply_text(
        "🖼️ Imágenes por sección:\n\n" + "\n".join(lineas) +
        "\n\nAsigna una con /imagenseccion <clave> y mandando la foto después."
    )

async def capturar_imagen_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Si un admin pidió /imagenregalo <id> o /imagenseccion <clave>, la siguiente
    foto que mande se asigna a ese regalo o a esa sección."""
    if not es_admin(update.effective_user.id):
        return
    db = load_db()
    cfg = db.setdefault("_config", {})

    seccion = cfg.get("esperando_imagen_seccion")
    if seccion:
        cfg.setdefault("imagenes", {})[seccion] = update.message.photo[-1].file_id
        cfg["esperando_imagen_seccion"] = None
        save_db(db)
        await update.message.reply_text(f"✅ Imagen guardada para la sección «{seccion}».")
        return

    id_ = cfg.get("esperando_imagen_regalo_id")
    if not id_:
        return
    regalo = next((g for g in db.get("regalos", []) if g["id"] == id_), None)
    if not regalo:
        cfg["esperando_imagen_regalo_id"] = None
        save_db(db)
        return
    regalo["imagen_file_id"] = update.message.photo[-1].file_id
    cfg["esperando_imagen_regalo_id"] = None
    save_db(db)
    await update.message.reply_text(f"✅ Imagen guardada para el regalo #{id_}.")

# ─── COMANDOS DE ADMINISTRACIÓN: GESTIÓN DE ADMINS ─────────────────
async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /addadmin <ID_telegram>")
        return
    try:
        nuevo_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El ID debe ser un número.")
        return
    db = load_db()
    extra = db.setdefault("_config", {}).setdefault("admin_extra", [])
    if nuevo_id in ADMIN_IDS or nuevo_id in extra:
        await update.message.reply_text("Ese ID ya es admin.")
        return
    extra.append(nuevo_id)
    save_db(db)
    await update.message.reply_text(f"✅ {nuevo_id} añadido como admin.")

async def cmd_deladmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /deladmin <ID_telegram>")
        return
    try:
        id_quitar = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El ID debe ser un número.")
        return
    if id_quitar in ADMIN_IDS:
        await update.message.reply_text("Ese admin está fijado en el código (ADMIN_IDS) y no se puede quitar por comando.")
        return
    db = load_db()
    extra = db.setdefault("_config", {}).setdefault("admin_extra", [])
    if id_quitar not in extra:
        await update.message.reply_text("Ese ID no estaba en la lista de admins añadidos por comando.")
        return
    extra.remove(id_quitar)
    save_db(db)
    await update.message.reply_text(f"✅ {id_quitar} eliminado de admins.")

async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    db = load_db()
    extra = db.get("_config", {}).get("admin_extra", [])
    lineas = [f"`{i}` (fijo en el código)" for i in ADMIN_IDS] + [f"`{i}` (añadido por comando)" for i in extra]
    await update.message.reply_text("👮 *Administradores actuales:*\n\n" + "\n".join(lineas), parse_mode="Markdown")

# ─── COMANDOS DE ADMINISTRACIÓN: NOMBRES DE LAS PESTAÑAS ───────────
async def cmd_nombres(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista las claves de cada pestaña y su nombre visible actual."""
    if not es_admin(update.effective_user.id):
        return
    db = load_db()
    et = get_etiquetas(db)
    lineas = [f"{clave} → {texto}" for clave, texto in et.items()]
    # Sin parse_mode: los nombres los puede editar el admin libremente, y si alguno
    # queda con un * o _ suelto, con Markdown el mensaje entero dejaría de enviarse.
    await update.message.reply_text(
        "🏷️ Pestañas del menú principal:\n\n" + "\n".join(lineas) +
        "\n\nCambia una con /setnombre <clave> <texto nuevo>\n"
        "Ej: /setnombre saldo 💎 OF Balance"
    )

async def cmd_setnombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cambia el nombre visible de una pestaña. Uso: /setnombre <clave> <texto nuevo>"""
    if not es_admin(update.effective_user.id):
        return
    partes = update.message.text.split(" ", 2)
    if len(partes) < 3 or not partes[2].strip():
        await update.message.reply_text(
            "Uso: /setnombre <clave> <texto nuevo>\n"
            "Usa /nombres para ver las claves disponibles."
        )
        return
    clave = partes[1]
    if clave not in ETIQUETAS_DEFECTO:
        await update.message.reply_text(
            "Esa clave no existe. Claves disponibles: " + ", ".join(ETIQUETAS_DEFECTO.keys())
        )
        return
    nuevo_texto = partes[2]
    db = load_db()
    db.setdefault("_config", {}).setdefault("etiquetas", {})[clave] = nuevo_texto
    save_db(db)
    await update.message.reply_text(f"✅ Pestaña «{clave}» renombrada a: {nuevo_texto}")

async def cmd_resetnombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Devuelve una pestaña a su nombre original. Uso: /resetnombre <clave>"""
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /resetnombre <clave>\nUsa /nombres para ver las claves disponibles.")
        return
    clave = context.args[0]
    if clave not in ETIQUETAS_DEFECTO:
        await update.message.reply_text(
            "Esa clave no existe. Claves disponibles: " + ", ".join(ETIQUETAS_DEFECTO.keys())
        )
        return
    db = load_db()
    db.setdefault("_config", {}).setdefault("etiquetas", {}).pop(clave, None)
    save_db(db)
    await update.message.reply_text(f"✅ Pestaña «{clave}» devuelta a su nombre original: {ETIQUETAS_DEFECTO[clave]}")

# ─── COMANDOS DE ADMINISTRACIÓN: TEXTOS VISUALES ───────────────────
async def cmd_textos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista las claves de cada texto visual y su contenido actual."""
    if not es_admin(update.effective_user.id):
        return
    db = load_db()
    lineas = []
    for clave in TEXTOS_DEFECTO:
        actual = get_texto(db, clave)
        resumen = actual.replace("\n", " ")[:60] + ("..." if len(actual) > 60 else "")
        lineas.append(f"{clave} → {resumen}")
    # Sin parse_mode a propósito: el resumen recorta el texto a 60 caracteres y, si
    # el corte cae en mitad de un *negrita* o _cursiva_, Telegram rechaza el mensaje
    # entero (el mismo problema que rompía /adminayuda). Al no usar Markdown aquí,
    # esto no puede volver a pasar por mucho que se edite el contenido.
    await update.message.reply_text(
        "📝 Textos visuales configurables:\n\n" + "\n".join(lineas) +
        "\n\nCambia uno con /settexto <clave> <texto nuevo>\n"
        "El de «titulo_cuentas» admite la variable $precio (no la borres si la usas)."
    )

async def cmd_settexto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cambia un texto visual. Uso: /settexto <clave> <texto nuevo> (admite varias líneas)."""
    if not es_admin(update.effective_user.id):
        return
    partes = update.message.text.split(" ", 2)
    if len(partes) < 3 or not partes[2].strip():
        await update.message.reply_text(
            "Uso: /settexto <clave> <texto nuevo>\n"
            "Usa /textos para ver las claves disponibles."
        )
        return
    clave = partes[1]
    if clave not in TEXTOS_DEFECTO:
        await update.message.reply_text(
            "Esa clave no existe. Claves disponibles: " + ", ".join(TEXTOS_DEFECTO.keys())
        )
        return
    nuevo_texto = partes[2]
    db = load_db()
    db.setdefault("_config", {}).setdefault("textos", {})[clave] = nuevo_texto
    save_db(db)
    await update.message.reply_text(f"✅ Texto «{clave}» actualizado.")

async def cmd_resettexto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Devuelve un texto visual a su versión original. Uso: /resettexto <clave>"""
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /resettexto <clave>\nUsa /textos para ver las claves disponibles.")
        return
    clave = context.args[0]
    if clave not in TEXTOS_DEFECTO:
        await update.message.reply_text(
            "Esa clave no existe. Claves disponibles: " + ", ".join(TEXTOS_DEFECTO.keys())
        )
        return
    db = load_db()
    db.setdefault("_config", {}).setdefault("textos", {}).pop(clave, None)
    save_db(db)
    await update.message.reply_text(f"✅ Texto «{clave}» devuelto a su versión original.")

# ─── COMANDOS DE ADMINISTRACIÓN: SUB-PESTAÑAS (botones dentro de cada categoría) ─
def _claves_validas_por_categoria(categoria):
    """Devuelve el conjunto de claves válidas y el nombre por defecto de cada una,
    según la categoría, para poder validar /setsubnombre."""
    if categoria == "saldo":
        return {clave: texto for clave, (texto, _p) in SALDO.items()}
    if categoria == "cc":
        return {clave: nombre for clave, (nombre, _p, _s) in ROPA.items()}
    if categoria == "cuentas":
        return {str(i): str(i) for i in range(1, NUM_PANTALONES + 1)}
    if categoria == "pedidos":
        d = {clave: nombre for clave, (nombre, _p) in PLATAFORMAS.items()}
        d.update({clave: nombre for clave, (nombre, _p, _pr) in EVENTOS.items()})
        return d
    return None

async def cmd_subnombres(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista las sub-pestañas (botones dentro de una categoría) y su nombre actual.
    Uso: /subnombres <categoria>  (categorías: saldo, cc, cuentas, pedidos)"""
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text(
            "Uso: /subnombres <categoria>\nCategorías disponibles: saldo, cc, cuentas, pedidos"
        )
        return
    categoria = context.args[0]
    defectos = _claves_validas_por_categoria(categoria)
    if defectos is None:
        await update.message.reply_text("Categoría no reconocida. Usa: saldo, cc, cuentas o pedidos.")
        return
    db = load_db()
    lineas = [f"{clave} → {get_subetiqueta(db, categoria, clave, defecto)}" for clave, defecto in defectos.items()]
    await update.message.reply_text(
        f"🏷️ Sub-pestañas de «{categoria}»:\n\n" + "\n".join(lineas) +
        f"\n\nCambia una con /setsubnombre {categoria} <clave> <texto nuevo>"
    )

async def cmd_setsubnombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cambia el nombre de una sub-pestaña. Uso: /setsubnombre <categoria> <clave> <texto nuevo>"""
    if not es_admin(update.effective_user.id):
        return
    partes = update.message.text.split(" ", 3)
    if len(partes) < 4 or not partes[3].strip():
        await update.message.reply_text(
            "Uso: /setsubnombre <categoria> <clave> <texto nuevo>\n"
            "Usa /subnombres <categoria> para ver las claves disponibles."
        )
        return
    categoria, clave, nuevo_texto = partes[1], partes[2], partes[3]
    defectos = _claves_validas_por_categoria(categoria)
    if defectos is None:
        await update.message.reply_text("Categoría no reconocida. Usa: saldo, cc, cuentas o pedidos.")
        return
    if clave not in defectos:
        await update.message.reply_text(
            f"Esa clave no existe en «{categoria}». Claves disponibles: " + ", ".join(defectos.keys())
        )
        return
    db = load_db()
    set_subetiqueta(db, categoria, clave, nuevo_texto)
    save_db(db)
    await update.message.reply_text(f"✅ «{categoria}:{clave}» renombrado a: {nuevo_texto}")

async def cmd_resetsubnombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Devuelve una sub-pestaña a su nombre original. Uso: /resetsubnombre <categoria> <clave>"""
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Uso: /resetsubnombre <categoria> <clave>")
        return
    categoria, clave = context.args
    defectos = _claves_validas_por_categoria(categoria)
    if defectos is None:
        await update.message.reply_text("Categoría no reconocida. Usa: saldo, cc, cuentas o pedidos.")
        return
    if clave not in defectos:
        await update.message.reply_text(
            f"Esa clave no existe en «{categoria}». Claves disponibles: " + ", ".join(defectos.keys())
        )
        return
    db = load_db()
    reset_subetiqueta(db, categoria, clave)
    save_db(db)
    await update.message.reply_text(f"✅ «{categoria}:{clave}» devuelto a su nombre original: {defectos[clave]}")

async def cmd_setpreciocuenta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cambia el precio de una opción concreta de Cuentas. Uso: /setpreciocuenta <numero> <precio>"""
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Uso: /setpreciocuenta <numero 1-8> <precio>\nEj: /setpreciocuenta 3 6.99")
        return
    numero, precio_txt = context.args
    if numero not in [str(i) for i in range(1, NUM_PANTALONES + 1)]:
        await update.message.reply_text(f"El número debe estar entre 1 y {NUM_PANTALONES}.")
        return
    try:
        precio = float(precio_txt.replace(",", "."))
    except ValueError:
        await update.message.reply_text("El precio debe ser un número.")
        return
    db = load_db()
    set_precio_cuenta(db, numero, precio)
    save_db(db)
    await update.message.reply_text(f"✅ Precio de la opción {numero} de Cuentas puesto a {precio:.2f}€.")

async def cmd_verpreciocuentas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    db = load_db()
    lineas = [f"{i} → {get_precio_cuenta(db, i):.2f}€" for i in range(1, NUM_PANTALONES + 1)]
    await update.message.reply_text(
        "💶 Precios de Cuentas:\n\n" + "\n".join(lineas) +
        "\n\nCambia uno con /setpreciocuenta <numero> <precio>"
    )

# ─── COMANDOS DE ADMINISTRACIÓN: ESTADÍSTICAS Y AVISOS ─────────────
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    db = load_db()
    n_usuarios = len(db.get("usuarios", {}))
    pedidos = db.get("pedidos", {})
    n_pedidos_activos = sum(1 for p in pedidos.values() if p["estado"] not in ("entregado", "cancelado"))
    n_pedidos_total = len(pedidos)
    compras = db.get("compras", {})
    n_compras_confirmadas = sum(1 for c in compras.values() if c["estado"] == "confirmada")
    ingresos_confirmados = sum(c["precio_eur"] for c in compras.values() if c["estado"] == "confirmada")
    n_compras_pendientes = sum(1 for c in compras.values() if c["estado"] != "confirmada")
    await update.message.reply_text(
        f"📊 *ESTADÍSTICAS GLOBALES*\n\n"
        f"👥 Usuarios registrados: {n_usuarios}\n"
        f"🍔 Pedidos: {n_pedidos_activos} activos / {n_pedidos_total} totales\n"
        f"🛍️ Compras confirmadas: {n_compras_confirmadas}\n"
        f"💶 Ingresos confirmados: {ingresos_confirmados:.2f}€\n"
        f"🕓 Compras pendientes de pago/verificación: {n_compras_pendientes}",
        parse_mode="Markdown"
    )

async def enviar_broadcast(context, db, mensaje):
    """Manda un mensaje a todos los usuarios registrados. Devuelve (enviados, fallidos)."""
    enviados, fallidos = 0, 0
    for uid in db.get("usuarios", {}):
        try:
            await context.bot.send_message(chat_id=int(uid), text=mensaje, parse_mode="Markdown")
            enviados += 1
        except Exception:
            fallidos += 1
    return enviados, fallidos

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    partes = update.message.text.split(" ", 1)
    if len(partes) < 2 or not partes[1].strip():
        await update.message.reply_text("Uso: /broadcast <mensaje>\nSe envía a todos los usuarios que hayan usado el bot.")
        return
    mensaje = partes[1]
    db = load_db()
    enviados, fallidos = await enviar_broadcast(context, db, mensaje)
    await update.message.reply_text(f"📣 Enviado a {enviados} usuarios ({fallidos} fallidos).")

async def cmd_avisarregalos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Avisa a todos de que hay regalos nuevos. Uso normal: lo lanzas tú manualmente
    cada domingo después de actualizar los regalos con /addregalo."""
    if not es_admin(update.effective_user.id):
        return
    db = load_db()
    mensaje = "🎁 *¡Nuevos regalos disponibles esta semana!*\n\nEntra en 🎁 Regalo Semanal para reclamarlos antes de que se agoten."
    enviados, fallidos = await enviar_broadcast(context, db, mensaje)
    await update.message.reply_text(f"📣 Aviso de regalos enviado a {enviados} usuarios ({fallidos} fallidos).")

# ─── COMANDOS DE ADMINISTRACIÓN: PEDIDOS ────────────────────────────
async def cmd_cancelarpedido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) < 1:
        await update.message.reply_text("Uso: /cancelarpedido <número> [motivo]")
        return
    numero = context.args[0]
    motivo = " ".join(context.args[1:])
    db = load_db()
    pedido = db["pedidos"].get(numero)
    if not pedido:
        await update.message.reply_text("No encuentro ese pedido.")
        return
    pedido["estado"] = "cancelado"
    save_db(db)
    try:
        await context.bot.send_message(
            chat_id=pedido["usuario_id"],
            text=f"❌ Tu pedido *#{numero}* ha sido cancelado." + (f"\nMotivo: {motivo}" if motivo else ""),
            parse_mode="Markdown"
        )
    except Exception:
        pass
    await update.message.reply_text(f"✅ Pedido #{numero} marcado como cancelado y cliente avisado.")

# ─── COMANDOS DE ADMINISTRACIÓN: STOCK DE CC ────────────────────────
async def cmd_addstock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /addstock <clave> <id1> <id2> ...\nClaves disponibles: " + ", ".join(ROPA.keys()))
        return
    clave = context.args[0]
    if clave not in ROPA:
        await update.message.reply_text("Clave no reconocida. Claves disponibles: " + ", ".join(ROPA.keys()))
        return
    _nombre, _precio, stock_file = ROPA[clave]
    nuevos_ids = context.args[1:]
    with open(stock_file, "a", encoding="utf-8") as f:
        for id_producto in nuevos_ids:
            f.write(id_producto + "\n")
    await update.message.reply_text(f"✅ Añadidos {len(nuevos_ids)} ID(s) a {clave}. Stock actual: {contar_stock(stock_file)}")

    db = load_db()
    nombre_mostrado = get_subetiqueta(db, "cc", clave, _nombre)
    mensaje = f"📦 *¡Nuevo stock disponible!*\n\nAcaba de entrar stock de *{nombre_mostrado}* en 🛒 Más productos → CC."
    enviados, fallidos = await enviar_broadcast(context, db, mensaje)
    await update.message.reply_text(f"📣 Aviso de stock nuevo enviado a {enviados} usuarios ({fallidos} fallidos).")

async def cmd_verstock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    lineas = [f"{nombre}: {contar_stock(stock_file)} unidad(es)" for _clave, (nombre, _precio, stock_file) in ROPA.items()]
    await update.message.reply_text("📦 *Stock CC:*\n\n" + "\n".join(lineas), parse_mode="Markdown")

# ─── COMANDOS DE ADMINISTRACIÓN: USUARIOS ───────────────────────────
async def cmd_resetxp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /resetxp <ID_usuario>")
        return
    uid = context.args[0]
    db = load_db()
    if uid not in db.get("usuarios", {}):
        await update.message.reply_text("No encuentro a ese usuario.")
        return
    u = get_usuario(db, uid)
    u["xp"] = 0
    u["claimed_levels"] = []
    u["entregado_levels"] = []
    save_db(db)
    await update.message.reply_text(f"✅ XP y niveles reiniciados para el usuario {uid}.")

async def cmd_adminayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not es_admin(update.effective_user.id):
        return
    texto = """
🛠️ *COMANDOS DE ADMIN*

*Premios de niveles*
/premios — ver premios configurados
/setpremio <nivel> <texto> — crear/editar un premio
/quitarpremio <nivel> — eliminar un premio
/pendientes — premios desbloqueados sin entregar
/entregar <ID> <nivel> — marcar premio como entregado

*Regalos aleatorios*
/verregalos — ver todos los regalos configurados
/addregalo <texto> — crear un regalo nuevo (empieza con 1 unidad)
/imagenregalo <id> — la próxima foto que mandes se asigna a ese regalo
/setcupogregalo <id> <n> — cambiar las unidades de un regalo (reinicia reclamos)
/quitarregalo <id> — eliminar un regalo por completo

*Pedidos*
/cola — ver pedidos activos
/cancelarpedido <número> [motivo] — cancelar un pedido y avisar al cliente

*Stock (CC)*
/addstock <clave> <id1> <id2> ... — añadir IDs de producto
/verstock — ver unidades restantes por producto

*General*
/stats — estadísticas globales del bot
/broadcast <mensaje> — enviar un aviso a todos los usuarios
/resetxp <ID usuario> — reiniciar el nivel/XP de un usuario

*Administradores*
/admins — ver quién es admin
/addadmin <ID> — añadir un admin
/deladmin <ID> — quitar un admin (solo los añadidos por comando)

*Nombres de las pestañas*
/nombres — ver las claves y el nombre actual de cada pestaña
/setnombre <clave> <texto nuevo> — renombrar una pestaña
/resetnombre <clave> — devolverla a su nombre original

*Textos visuales (cabeceras, bienvenida...)*
/textos — ver las claves y el texto actual de cada una
/settexto <clave> <texto nuevo> — cambiar un texto
/resettexto <clave> — devolverlo a su versión original

*Sub-pestañas (botones dentro de cada categoría)*
/subnombres <categoria> — ver claves y nombres (saldo, cc, cuentas, pedidos)
/setsubnombre <categoria> <clave> <texto nuevo> — renombrar una
/resetsubnombre <categoria> <clave> — devolverla a su nombre original

*Precios de Cuentas*
/verpreciocuentas — ver el precio de cada opción (1-8)
/setpreciocuenta <numero> <precio> — cambiar el precio de una opción

*Imágenes por sección*
/verimagenes — ver qué secciones tienen imagen
/imagenseccion <clave> — la próxima foto que mandes se asigna a esa sección
/quitarimagenseccion <clave> — quitar la imagen de una sección

*Regalo Semanal*
/avisarregalos — avisar a todos de que hay regalos nuevos

*Tickets de soporte*
/tickets — ver los tickets abiertos
(el estado se cambia con los botones del mensaje que llega al grupo)
"""
    await update.message.reply_text(texto, parse_mode="Markdown")

# ─── CATEGORÍA: PEDIDOS A DOMICILIO ───────────────────────────────
async def categoria_pedidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    await mostrar_seccion(q, context, "titulo_pedidos", get_texto(db, "titulo_pedidos"), teclado_pedidos_menu())

# ─── CATEGORÍA: ROPA ───────────────────────────────────────────────
async def categoria_ropa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    await mostrar_seccion(q, context, "titulo_cc", get_texto(db, "titulo_cc"), teclado_ropa_menu(db))

async def ver_producto_ropa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    clave = q.data.split("_", 1)[1]
    context.user_data["cc_esperando_codigo"] = clave
    await q.edit_message_text(
        "🔢 Escribe el *código de referencia* del producto (6 dígitos).",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")]])
    )

async def capturar_codigo_cc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captura el código de 6 dígitos que el cliente escribe tras pulsar Camisetas/Pantalones.
    Si no se estaba esperando ningún código (fuera de ese flujo), no hace nada."""
    clave = context.user_data.get("cc_esperando_codigo")
    if not clave:
        return

    codigo = update.message.text.strip()
    if not (codigo.isdigit() and len(codigo) == 6):
        await update.message.reply_text(
            "❌ El código debe tener exactamente 6 dígitos. Vuelve a escribirlo.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")]])
        )
        return  # se mantiene la espera para que pueda reintentar

    db = load_db()
    nombre_defecto, precio, stock_file = ROPA[clave]
    nombre = get_subetiqueta(db, "cc", clave, nombre_defecto)
    cantidad = contar_stock_por_codigo(stock_file, codigo)

    if cantidad <= 0:
        await update.message.reply_text(
            f"❌ El ID *{codigo}* no está disponible.\nPrueba con otro código.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")]])
        )
        return  # se mantiene la espera para que pueda reintentar con otro código

    context.user_data.pop("cc_esperando_codigo", None)
    await update.message.reply_text(
        f"✅ *{nombre}* — código *{codigo}*\n\nHay *{cantidad}* unidad(es) en stock.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Comprar", callback_data=f"cccomprar_{clave}_{codigo}")],
            [InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")],
        ])
    )

async def cc_comprar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, clave, codigo = q.data.split("_")
    db = load_db()
    nombre_defecto, precio, stock_file = ROPA[clave]
    nombre = get_subetiqueta(db, "cc", clave, nombre_defecto)

    # Se revalida el stock por si otra persona ha comprado la última unidad justo antes.
    if contar_stock_por_codigo(stock_file, codigo) <= 0:
        await q.edit_message_text(
            f"❌ El ID *{codigo}* ya no está disponible.",
            parse_mode="Markdown",
            reply_markup=volver_menu_keyboard()
        )
        return

    compra_id = crear_compra(
        db, q.from_user.id, q.from_user.username or q.from_user.first_name,
        f"{nombre} (ID {codigo})", precio, auto_entrega=True, stock_file=stock_file, stock_codigo=codigo
    )
    await iniciar_pago(q, context, compra_id, precio, f"{nombre} (ID {codigo})")

# ─── CATEGORÍA: PANTALONES ──────────────────────────────────────────
# 8 opciones numeradas, todas al mismo precio (da igual cuál se elija).
NUM_PANTALONES = 8
PRECIO_PANTALONES = 4.99  # precio por defecto; cada opción puede tener el suyo propio

def get_precio_cuenta(db, numero):
    return db.get("_config", {}).get("precios_cuentas", {}).get(str(numero), PRECIO_PANTALONES)

def set_precio_cuenta(db, numero, precio):
    db.setdefault("_config", {}).setdefault("precios_cuentas", {})[str(numero)] = precio

def reset_precio_cuenta(db, numero):
    db.setdefault("_config", {}).setdefault("precios_cuentas", {}).pop(str(numero), None)

def teclado_pantalones_menu(db):
    # Sin precio en el botón a propósito: el precio de cada opción aparece al pulsarla.
    filas = [
        [InlineKeyboardButton(get_subetiqueta(db, 'cuentas', str(i), str(i)), callback_data=f"pantalon_{i}")]
        for i in range(1, NUM_PANTALONES + 1)
    ]
    filas.append([InlineKeyboardButton("🔙 Volver", callback_data="cat_mas_productos")])
    return InlineKeyboardMarkup(filas)

async def categoria_pantalones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    await mostrar_seccion(q, context, "titulo_cuentas", get_texto(db, "titulo_cuentas"), teclado_pantalones_menu(db))

async def ver_pantalon_numero(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    numero = q.data.split("_", 1)[1]
    db = load_db()
    etiqueta = get_subetiqueta(db, "cuentas", numero, numero)
    precio = get_precio_cuenta(db, numero)
    compra_id = crear_compra(
        db, q.from_user.id, q.from_user.username or q.from_user.first_name,
        f"Cuentas (opción {etiqueta})", precio
    )
    await iniciar_pago(q, context, compra_id, precio, f"Cuentas (opción {etiqueta})")

# ─── CATEGORÍA: SALDO ──────────────────────────────────────────────
async def categoria_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    await mostrar_seccion(q, context, "titulo_saldo", get_texto(db, "titulo_saldo"), teclado_saldo_menu(db))

async def ver_producto_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    texto_defecto, precio = SALDO[q.data]
    texto_opcion = get_subetiqueta(db, "saldo", q.data, texto_defecto)

    if precio is None:
        await q.edit_message_text(
            f"💰 *{texto_opcion}*\n\nEsta opción no tiene coste asociado. Si tienes dudas, contacta con soporte.",
            parse_mode="Markdown",
            reply_markup=volver_menu_keyboard()
        )
        return

    compra_id = crear_compra(db, q.from_user.id, q.from_user.username or q.from_user.first_name, f"Saldo {texto_opcion}", precio)
    await iniciar_pago(q, context, compra_id, precio, f"Saldo {texto_opcion}")

# ─── CATEGORÍA: TARGET ──────────────────────────────────────────────
PRECIO_TARGET = 4.99

async def categoria_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    compra_id = crear_compra(db, q.from_user.id, q.from_user.username or q.from_user.first_name, "eSIM", PRECIO_TARGET)
    await iniciar_pago(q, context, compra_id, PRECIO_TARGET, "eSIM")

# ─── CATEGORÍA: REGALOS ALEATORIOS ─────────────────────────────────
# Cada regalo es independiente (su propio texto/imagen/cupo). Si hay varios
# disponibles a la vez, se navegan con ⬅️/➡️. Al reclamarse, ese regalo
# concreto se borra del chat (mensaje y foto incluidos) y desaparece de la
# lista si se agota su cupo. No es "uno por persona": es un cupo compartido
# por regalo, para los primeros que lo reclamen.
def tiempo_hasta_proximo_regalo():
    """Cuenta atrás hasta el próximo domingo a las 23:59 (hora de España)."""
    tz = ZoneInfo("Europe/Madrid")
    ahora = datetime.now(tz)
    dias_hasta_domingo = (6 - ahora.weekday()) % 7  # weekday(): lunes=0 ... domingo=6
    objetivo = (ahora + timedelta(days=dias_hasta_domingo)).replace(hour=23, minute=59, second=0, microsecond=0)
    if objetivo <= ahora:
        objetivo += timedelta(days=7)
    restante = objetivo - ahora
    dias, resto_segundos = restante.days, restante.seconds
    horas, resto_segundos = divmod(resto_segundos, 3600)
    minutos = resto_segundos // 60
    return f"{dias}d {horas}h {minutos}min"

async def _mostrar_regalo_en_indice(q, context, indice):
    db = load_db()
    disponibles = [g for g in db.get("regalos", []) if g.get("cupo_restante", 0) > 0]
    countdown = f"\n\n⏳ Próxima actualización de regalos en: *{tiempo_hasta_proximo_regalo()}*"

    if not disponibles:
        try:
            await q.edit_message_text(
                "🎁 *Regalo Semanal*\n\n"
                "Ahora mismo no hay ningún regalo disponible.\n"
                "Los regalos se actualizan todos los domingos a las 23:59 (hora de España)."
                + countdown,
                parse_mode="Markdown",
                reply_markup=volver_menu_keyboard()
            )
        except Exception:
            pass
        return

    indice = max(0, min(indice, len(disponibles) - 1))
    regalo = disponibles[indice]
    uid = q.from_user.id
    ya_reclamado = uid in regalo.get("reclamado_por", [])

    texto = regalo.get("texto") or "¡Hay un regalo disponible!"
    cabecera = f"🎁 *Regalo {indice + 1}/{len(disponibles)}*\n\n{texto}\n\n🏃 Quedan *{regalo['cupo_restante']}* unidad(es)."
    if ya_reclamado:
        cabecera += "\n\n✅ Ya reclamaste este regalo."
    cabecera += countdown

    fila_nav = []
    if indice > 0:
        fila_nav.append(InlineKeyboardButton("⬅️", callback_data=f"regalo_ver_{indice - 1}"))
    if indice < len(disponibles) - 1:
        fila_nav.append(InlineKeyboardButton("➡️", callback_data=f"regalo_ver_{indice + 1}"))

    filas = []
    if fila_nav:
        filas.append(fila_nav)
    if not ya_reclamado:
        filas.append([InlineKeyboardButton("🎉 Reclamar", callback_data=f"regalo_reclamar_{regalo['id']}")])
    filas.append([InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")])
    kb = InlineKeyboardMarkup(filas)

    imagen = regalo.get("imagen_file_id")
    es_foto_actual = bool(q.message.photo)

    # No se puede "editar" un mensaje de texto para volverlo foto (ni al revés),
    # así que en esos casos se borra y se manda uno nuevo del tipo que toque.
    if imagen and not es_foto_actual:
        try:
            await q.message.delete()
        except Exception:
            pass
        await context.bot.send_photo(chat_id=uid, photo=imagen, caption=cabecera, parse_mode="Markdown", reply_markup=kb)
    elif imagen and es_foto_actual:
        await q.edit_message_caption(caption=cabecera, parse_mode="Markdown", reply_markup=kb)
    elif not imagen and es_foto_actual:
        try:
            await q.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=uid, text=cabecera, parse_mode="Markdown", reply_markup=kb)
    else:
        await q.edit_message_text(cabecera, parse_mode="Markdown", reply_markup=kb)

async def categoria_regalo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _mostrar_regalo_en_indice(q, context, 0)

async def regalo_ver_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    indice = int(q.data.split("_")[-1])
    await _mostrar_regalo_en_indice(q, context, indice)

async def regalo_reclamar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    regalo_id = int(q.data.split("_")[-1])
    db = load_db()
    regalo = next((g for g in db.get("regalos", []) if g["id"] == regalo_id), None)
    uid = q.from_user.id

    if not regalo or regalo.get("cupo_restante", 0) <= 0:
        await q.answer("Este regalo ya no está disponible.", show_alert=True)
        return
    if uid in regalo.get("reclamado_por", []):
        await q.answer("Ya lo reclamaste antes.", show_alert=True)
        return

    regalo.setdefault("reclamado_por", []).append(uid)
    regalo["cupo_restante"] -= 1
    save_db(db)

    # Se borra el mensaje del regalo (texto o foto) tal como se pidió, en vez de dejarlo editado.
    try:
        await q.message.delete()
    except Exception:
        pass

    texto_final = f"🎉 *¡Regalo reclamado!*\n\n{regalo.get('texto', '')}\n\nTe lo enviamos por privado, ¡disfrútalo!"
    imagen = regalo.get("imagen_file_id")
    if imagen:
        await context.bot.send_photo(
            chat_id=uid, photo=imagen, caption=texto_final,
            parse_mode="Markdown", reply_markup=volver_menu_keyboard()
        )
    else:
        await context.bot.send_message(
            chat_id=uid, text=texto_final,
            parse_mode="Markdown", reply_markup=volver_menu_keyboard()
        )

    try:
        await context.bot.send_message(
            chat_id=GRUPO_ADMIN_ID,
            text=(
                f"🎁 @{q.from_user.username or q.from_user.first_name} (ID `{uid}`) reclamó el regalo #{regalo_id}.\n"
                f"Quedan {regalo['cupo_restante']} unidad(es) de ese regalo."
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass

# ─── VER ESTADO DEL PEDIDO (botón, no comando) ─────────────────────
async def ver_estado_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    db = load_db()
    uid = q.from_user.id
    pedidos_usuario = [
        (int(num), p) for num, p in db["pedidos"].items()
        if p["usuario_id"] == uid and p["estado"] not in ("entregado", "cancelado")
    ]

    if not pedidos_usuario:
        await q.edit_message_text(
            "No tienes ningún pedido activo ahora mismo.",
            reply_markup=menu_principal()
        )
        return

    pedidos_usuario.sort()
    lineas = []
    for numero, p in pedidos_usuario:
        pendientes_antes = sum(
            1 for n2, p2 in db["pedidos"].items()
            if int(n2) < numero and p2["estado"] not in ("entregado", "cancelado")
        )
        lineas.append(
            f"📦 *Pedido #{numero}* ({p.get('restaurante_nombre', '?')})\n"
            f"Estado: {ESTADOS_PEDIDO[p['estado']]}\n"
            f"Pedidos por delante: {pendientes_antes}"
        )

    await q.edit_message_text(
        "\n\n".join(lineas),
        parse_mode="Markdown",
        reply_markup=volver_menu_keyboard()
    )

# ─── FLUJO DE PEDIDO ────────────────────────────────────────────────
async def pedido_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    db = load_db()
    await q.edit_message_text(
        "🍽️ *¿Dónde quieres pedir?*",
        parse_mode="Markdown",
        reply_markup=teclado_restaurantes(db)
    )
    return RESTAURANTE

async def seleccionar_restaurante_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    clave = q.data.split("_", 1)[1]

    if clave == "personalizado":
        context.user_data["restaurante_clave"] = "personalizado"
        context.user_data["restaurante_nombre"] = "Pedido personalizado"
        context.user_data["comision_texto"] = f"{PRECIO_PERSONALIZADO:.2f}€ (precio fijo)"
        context.user_data["precio_eur"] = PRECIO_PERSONALIZADO
        context.user_data["precio_fijo"] = True
        context.user_data["es_evento"] = False
        await q.edit_message_text(
            f"✏️ *Pedido personalizado* — {PRECIO_PERSONALIZADO:.2f}€\n\n"
            "Cuéntanos qué quieres pedir y dónde (restaurante, tienda, plataforma...).",
            parse_mode="Markdown",
            reply_markup=cancelar_conv_keyboard()
        )
        return PEDIDO

    db = load_db()
    nombre_defecto, precio = PLATAFORMAS[clave]
    nombre = get_subetiqueta(db, "pedidos", clave, nombre_defecto)
    context.user_data["restaurante_clave"] = clave
    context.user_data["restaurante_nombre"] = nombre
    context.user_data["comision_texto"] = f"{precio:.2f}€ (precio fijo)"
    context.user_data["precio_eur"] = precio
    context.user_data["precio_fijo"] = True
    context.user_data["es_evento"] = False

    await q.edit_message_text(
        f"🍔 *Pedido en {nombre}* ({precio:.2f}€)\n\n"
        f"Escribe qué quieres pedir (ej. _2 hamburguesas, 1 patatas grandes, 1 refresco_).",
        parse_mode="Markdown",
        reply_markup=cancelar_conv_keyboard()
    )
    return PEDIDO

async def seleccionar_evento_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    clave = q.data.split("_", 1)[1]
    db = load_db()
    nombre_defecto, precio, pregunta = EVENTOS[clave]
    nombre = get_subetiqueta(db, "pedidos", clave, nombre_defecto)
    context.user_data["restaurante_clave"] = clave
    context.user_data["restaurante_nombre"] = nombre
    context.user_data["comision_texto"] = f"{precio:.2f}€ (precio fijo)"
    context.user_data["precio_eur"] = precio
    context.user_data["precio_fijo"] = True
    context.user_data["es_evento"] = True

    await q.edit_message_text(
        f"🎟️ *{nombre}* — {precio:.2f}€\n\n{pregunta}",
        parse_mode="Markdown",
        reply_markup=cancelar_conv_keyboard()
    )
    return PEDIDO

async def recibir_pedido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pedido"] = update.message.text
    await update.message.reply_text(
        "👤 ¿Cuál es tu *nombre completo*?",
        parse_mode="Markdown",
        reply_markup=cancelar_conv_keyboard()
    )
    return NOMBRE

async def recibir_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nombre"] = update.message.text
    await update.message.reply_text(
        "📧 ¿Cuál es tu *email*? (obligatorio)",
        parse_mode="Markdown",
        reply_markup=cancelar_conv_keyboard()
    )
    return EMAIL

async def recibir_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["email"] = update.message.text

    if context.user_data.get("es_evento"):
        # Cinesa / Fever-Venues: no hace falta teléfono, dirección ni comentarios.
        context.user_data["telefono"] = "-"
        context.user_data["direccion"] = "-"
        context.user_data["comentarios"] = "-"
        return await mostrar_resumen(update.message, context)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ No quiero dar mi teléfono", callback_data="saltar_telefono")],
        [InlineKeyboardButton("❌ Cancelar pedido", callback_data="cancelar_conv")]
    ])
    await update.message.reply_text("📞 ¿Cuál es tu *teléfono de contacto*?", parse_mode="Markdown", reply_markup=kb)
    return TELEFONO

async def recibir_telefono(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["telefono"] = update.message.text
    await update.message.reply_text(
        "📍 ¿Cuál es la *dirección de entrega*?",
        parse_mode="Markdown",
        reply_markup=cancelar_conv_keyboard()
    )
    return DIRECCION

async def saltar_telefono_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["telefono"] = "-"
    await q.edit_message_text(
        "📍 ¿Cuál es la *dirección de entrega*?",
        parse_mode="Markdown",
        reply_markup=cancelar_conv_keyboard()
    )
    return DIRECCION

async def recibir_direccion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["direccion"] = update.message.text
    return await pedir_comentarios(update.message, context)

async def pedir_comentarios(message, context, editar=False):
    texto = "💬 ¿Algún *comentario para el repartidor*? (portal, piso, referencias...)"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ Sin comentarios", callback_data="saltar_comentarios")],
        [InlineKeyboardButton("❌ Cancelar pedido", callback_data="cancelar_conv")]
    ])
    if editar:
        await message.edit_text(texto, parse_mode="Markdown", reply_markup=kb)
    else:
        await message.reply_text(texto, parse_mode="Markdown", reply_markup=kb)
    return COMENTARIOS

async def recibir_comentarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["comentarios"] = update.message.text
    if context.user_data.get("precio_fijo"):
        return await mostrar_resumen(update.message, context)
    return await pedir_precio_pedido(update.message, context)

async def saltar_comentarios_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["comentarios"] = "-"
    if context.user_data.get("precio_fijo"):
        return await mostrar_resumen(q.message, context, editar=True)
    return await pedir_precio_pedido(q.message, context, editar=True)

async def pedir_precio_pedido(message, context, editar=False):
    texto = "💶 ¿Cuál es el *importe total a pagar* por este pedido (en €)?\n\nEscribe solo el número, ej. _23.50_"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar pedido", callback_data="cancelar_conv")]])
    if editar:
        await message.edit_text(texto, parse_mode="Markdown", reply_markup=kb)
    else:
        await message.reply_text(texto, parse_mode="Markdown", reply_markup=kb)
    return PRECIO

async def recibir_precio_pedido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip().replace(",", ".")
    try:
        precio = float(texto)
        if precio <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Ese importe no es válido. Escribe solo un número, ej. _23.50_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar pedido", callback_data="cancelar_conv")]])
        )
        return PRECIO
    context.user_data["precio_eur"] = round(precio, 2)
    return await mostrar_resumen(update.message, context)

async def mostrar_resumen(message, context, editar=False):
    d = context.user_data
    texto = f"""
📋 *REVISA TU PEDIDO*

🏪 Restaurante: {d['restaurante_nombre']} ({d['comision_texto']})
🍽️ Pedido: {d['pedido']}
👤 Nombre: {d['nombre']}
📧 Email: {d['email']}
📞 Teléfono: {d['telefono']}
📍 Dirección: {d['direccion']}
💬 Comentarios: {d['comentarios']}
💶 Importe total: {d['precio_eur']:.2f}€

¿Todo correcto?
"""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmar pedido", callback_data="confirmar_pedido"),
         InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_conv")]
    ])
    if editar:
        await message.edit_text(texto, parse_mode="Markdown", reply_markup=kb)
    else:
        await message.reply_text(texto, parse_mode="Markdown", reply_markup=kb)
    return CONFIRMAR

async def cancelar_conv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    db = load_db()
    await q.edit_message_text(get_texto(db, "bienvenida"), parse_mode="Markdown", reply_markup=menu_principal())
    return ConversationHandler.END

# ─── CONFIRMACIÓN Y ENVÍO AL GRUPO DE ADMINS ──────────────────────
def texto_para_admin(numero, d):
    return f"""
🔔 *PEDIDO #{numero}*

👤 *Solicitado por:* @{d['username']} (ID `{d['usuario_id']}`)

🏪 *Restaurante:* {d['restaurante_nombre']} ({d['comision_texto']})
🍽️ *Pedido:* {d['pedido']}
👤 *Nombre:* {d['nombre']}
📧 *Email:* {d['email']}
📞 *Teléfono:* {d['telefono']}
📍 *Dirección:* {d['direccion']}
💬 *Comentarios:* {d['comentarios']}
💶 *Importe:* {d['precio_eur']:.2f}€

📌 Estado: {ESTADOS_PEDIDO[d['estado']]}
⚠️ El pago se gestiona en un mensaje aparte (compra vinculada); revisa que esté confirmado antes de dar por hecho el cobro.
"""

async def confirmar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    db = load_db()
    db["ultimo_numero"] += 1
    numero = db["ultimo_numero"]

    d = dict(context.user_data)
    d["usuario_id"] = q.from_user.id
    d["username"] = q.from_user.username or q.from_user.first_name
    d["estado"] = "recibido"
    d["creado_at"] = datetime.now().isoformat()
    d["mensaje_admin_id"] = None

    db["pedidos"][str(numero)] = d
    save_db(db)

    try:
        msg = await context.bot.send_message(
            chat_id=GRUPO_ADMIN_ID,
            text=texto_para_admin(numero, d),
            parse_mode="Markdown",
            reply_markup=teclado_admin(numero)
        )
        db["pedidos"][str(numero)]["mensaje_admin_id"] = msg.message_id
        save_db(db)
    except Exception as e:
        logging.error(f"No se pudo enviar el pedido #{numero} al grupo de admins: {e}")

    compra_id = crear_compra(
        load_db(), q.from_user.id, d["username"],
        f"Pedido #{numero} — {d['restaurante_nombre']}", d["precio_eur"]
    )

    await q.edit_message_text(
        f"✅ *¡Pedido confirmado!*\n\n"
        f"Tu número de cola es *#{numero}*.\n"
        f"Te avisaremos aquí mismo según vaya avanzando tu pedido.\n\n"
        f"⚠️ *IMPORTANTE:* tu pedido NO se completa hasta que realices el pago.",
        parse_mode="Markdown"
    )
    await iniciar_pago(q, context, compra_id, d["precio_eur"], f"Pedido #{numero}")

    context.user_data.clear()
    return ConversationHandler.END

# ─── CAMBIO DE ESTADO DESDE EL GRUPO DE ADMINS ────────────────────
async def estado_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, numero_str, nuevo_estado = q.data.split("_")
    db = load_db()
    pedido = db["pedidos"].get(numero_str)
    if not pedido:
        await q.answer("Pedido no encontrado.", show_alert=True)
        return

    pedido["estado"] = nuevo_estado
    save_db(db)

    nuevo_texto = texto_para_admin(numero_str, pedido)
    await q.edit_message_text(nuevo_texto, parse_mode="Markdown", reply_markup=teclado_admin(numero_str))

    try:
        await context.bot.send_message(
            chat_id=pedido["usuario_id"],
            text=f"📦 Tu pedido *#{numero_str}* ahora está: {ESTADOS_PEDIDO[nuevo_estado]}",
            parse_mode="Markdown"
        )
    except Exception:
        pass

# ─── COMANDO PARA ADMINS: VER LA COLA COMPLETA ────────────────────
# Este se deja como comando (/cola) porque es de uso interno del equipo, no del cliente.
async def cmd_cola(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    activos = [
        (int(num), p) for num, p in db["pedidos"].items()
        if p["estado"] not in ("entregado", "cancelado")
    ]
    if not activos:
        await update.message.reply_text("✅ No hay pedidos pendientes.")
        return
    activos.sort()
    lineas = [f"#{n} — {p['nombre']} ({p.get('restaurante_nombre','?')}) — {ESTADOS_PEDIDO[p['estado']]}" for n, p in activos]
    await update.message.reply_text("📋 *Cola actual:*\n\n" + "\n".join(lineas), parse_mode="Markdown")

# ─── MAIN ─────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(pedido_inicio, pattern="^nuevo_pedido$")],
        states={
            RESTAURANTE: [
                CallbackQueryHandler(seleccionar_restaurante_callback, pattern="^restaurante_"),
                CallbackQueryHandler(seleccionar_evento_callback, pattern="^evento_"),
                CallbackQueryHandler(cancelar_conv_callback, pattern="^cancelar_conv$"),
            ],
            PEDIDO: [
                CallbackQueryHandler(cancelar_conv_callback, pattern="^cancelar_conv$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_pedido),
            ],
            NOMBRE: [
                CallbackQueryHandler(cancelar_conv_callback, pattern="^cancelar_conv$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_nombre),
            ],
            EMAIL: [
                CallbackQueryHandler(cancelar_conv_callback, pattern="^cancelar_conv$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_email),
            ],
            TELEFONO: [
                CallbackQueryHandler(saltar_telefono_callback, pattern="^saltar_telefono$"),
                CallbackQueryHandler(cancelar_conv_callback, pattern="^cancelar_conv$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_telefono),
            ],
            DIRECCION: [
                CallbackQueryHandler(cancelar_conv_callback, pattern="^cancelar_conv$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_direccion),
            ],
            COMENTARIOS: [
                CallbackQueryHandler(saltar_comentarios_callback, pattern="^saltar_comentarios$"),
                CallbackQueryHandler(cancelar_conv_callback, pattern="^cancelar_conv$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_comentarios),
            ],
            PRECIO: [
                CallbackQueryHandler(cancelar_conv_callback, pattern="^cancelar_conv$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_precio_pedido),
            ],
            CONFIRMAR: [
                CallbackQueryHandler(confirmar_callback, pattern="^confirmar_pedido$"),
                CallbackQueryHandler(cancelar_conv_callback, pattern="^cancelar_conv$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancelar_conv_callback, pattern="^cancelar_conv$"),
            CommandHandler("menu", cmd_menu),
        ],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(categoria_pedidos, pattern="^cat_pedidos$"))
    app.add_handler(CallbackQueryHandler(categoria_ropa, pattern="^cat_ropa$"))
    app.add_handler(CallbackQueryHandler(ver_producto_ropa, pattern="^ropa_"))
    app.add_handler(CallbackQueryHandler(cc_comprar_callback, pattern="^cccomprar_"))
    app.add_handler(CallbackQueryHandler(categoria_pantalones, pattern="^cat_pantalones$"))
    app.add_handler(CallbackQueryHandler(ver_pantalon_numero, pattern="^pantalon_"))
    app.add_handler(CallbackQueryHandler(categoria_saldo, pattern="^cat_saldo$"))
    app.add_handler(CallbackQueryHandler(ver_producto_saldo, pattern="^saldo_"))
    app.add_handler(CallbackQueryHandler(categoria_target, pattern="^cat_target$"))
    app.add_handler(CallbackQueryHandler(categoria_perfil, pattern="^cat_perfil$"))
    app.add_handler(CallbackQueryHandler(perfil_estadisticas_callback, pattern="^perfil_estadisticas$"))
    app.add_handler(CallbackQueryHandler(perfil_referidos_callback, pattern="^perfil_referidos$"))
    app.add_handler(CallbackQueryHandler(perfil_pedidos_callback, pattern="^perfil_pedidos$"))
    app.add_handler(CallbackQueryHandler(ref_enlace_callback, pattern="^ref_enlace$"))
    app.add_handler(CallbackQueryHandler(ref_verificar_callback, pattern="^ref_verificar$"))
    app.add_handler(CallbackQueryHandler(ref_ranking_callback, pattern="^ref_ranking$"))
    app.add_handler(CommandHandler("setpremio", cmd_setpremio))
    app.add_handler(CommandHandler("quitarpremio", cmd_quitarpremio))
    app.add_handler(CommandHandler("premios", cmd_premios))
    app.add_handler(CommandHandler("pendientes", cmd_pendientes))
    app.add_handler(CommandHandler("entregar", cmd_entregar))
    app.add_handler(CommandHandler("addregalo", cmd_addregalo))
    app.add_handler(CommandHandler("quitarregalo", cmd_quitarregalo))
    app.add_handler(CommandHandler("setcupogregalo", cmd_setcupogregalo))
    app.add_handler(CommandHandler("imagenregalo", cmd_imagenregalo))
    app.add_handler(CommandHandler("verregalos", cmd_verregalos))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("deladmin", cmd_deladmin))
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CommandHandler("nombres", cmd_nombres))
    app.add_handler(CommandHandler("setnombre", cmd_setnombre))
    app.add_handler(CommandHandler("resetnombre", cmd_resetnombre))
    app.add_handler(CommandHandler("textos", cmd_textos))
    app.add_handler(CommandHandler("settexto", cmd_settexto))
    app.add_handler(CommandHandler("resettexto", cmd_resettexto))
    app.add_handler(CommandHandler("subnombres", cmd_subnombres))
    app.add_handler(CommandHandler("setsubnombre", cmd_setsubnombre))
    app.add_handler(CommandHandler("resetsubnombre", cmd_resetsubnombre))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("cancelarpedido", cmd_cancelarpedido))
    app.add_handler(CommandHandler("addstock", cmd_addstock))
    app.add_handler(CommandHandler("verstock", cmd_verstock))
    app.add_handler(CommandHandler("resetxp", cmd_resetxp))
    app.add_handler(CommandHandler("imagenseccion", cmd_imagenseccion))
    app.add_handler(CommandHandler("quitarimagenseccion", cmd_quitarimagenseccion))
    app.add_handler(CommandHandler("verimagenes", cmd_verimagenes))
    app.add_handler(CommandHandler("setpreciocuenta", cmd_setpreciocuenta))
    app.add_handler(CommandHandler("verpreciocuentas", cmd_verpreciocuentas))
    app.add_handler(CommandHandler("avisarregalos", cmd_avisarregalos))
    app.add_handler(CommandHandler("tickets", cmd_tickets))
    app.add_handler(CallbackQueryHandler(categoria_mas_productos, pattern="^cat_mas_productos$"))
    app.add_handler(CallbackQueryHandler(categoria_soporte, pattern="^cat_soporte$"))
    app.add_handler(CallbackQueryHandler(ticket_nuevo_callback, pattern="^ticket_nuevo$"))
    app.add_handler(CallbackQueryHandler(ticketestado_callback, pattern="^ticketestado_"))
    app.add_handler(CommandHandler("adminayuda", cmd_adminayuda))
    app.add_handler(CallbackQueryHandler(categoria_regalo, pattern="^cat_regalo$"))
    app.add_handler(CallbackQueryHandler(regalo_ver_callback, pattern="^regalo_ver_"))
    app.add_handler(CallbackQueryHandler(regalo_reclamar_callback, pattern="^regalo_reclamar_"))
    app.add_handler(MessageHandler(filters.PHOTO, capturar_imagen_admin))
    app.add_handler(CallbackQueryHandler(pago_cripto_callback, pattern="^pagocrypto_"))
    app.add_handler(CallbackQueryHandler(pago_transferencia_callback, pattern="^pagotransfer_"))
    app.add_handler(CallbackQueryHandler(pago_moneda_callback, pattern="^pagomoneda_"))
    app.add_handler(CallbackQueryHandler(volver_metodo_pago_callback, pattern="^volverpago_"))
    app.add_handler(CallbackQueryHandler(compra_confirmar_callback, pattern="^compraconfirmar_"))
    app.add_handler(CallbackQueryHandler(ver_estado_callback, pattern="^ver_estado$"))
    app.add_handler(CallbackQueryHandler(volver_al_menu, pattern="^menu_principal$"))
    app.add_handler(CallbackQueryHandler(estado_callback, pattern="^estado_"))
    app.add_handler(CommandHandler("cola", cmd_cola))
    # Captura el hash de la transacción cripto cuando el usuario lo envía como texto suelto
    # (fuera de la conversación de pedido; solo actúa si hay una compra esperando ese hash).
    # Se registran en grupos distintos para que Telegram evalúe los DOS, no solo el primero
    # que coincida (cada uno comprueba su propia bandera en user_data y no hace nada si no aplica).
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_codigo_cc), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_hash_pago), group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_ticket), group=3)

    print("🛍️ Bot Zero Shop arrancado...")
    app.run_polling()

if __name__ == "__main__":
    main()
