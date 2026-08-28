import logging
import os
import json
import asyncio
import urllib.request
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes
)

# ─── CONFIGURACIÓN ────────────────────────────────────────────────
BOT_TOKEN = "8672882435:AAHECP0xEWorI9lQSc4R4JixIhnRXnzZ5j4"

# ID del grupo de administradores donde llegan los pedidos.
GRUPO_ADMIN_ID = -1004416626509

DB_FILE = os.environ.get("DB_PATH", "pedidos.json")

logging.basicConfig(level=logging.INFO)

# ─── PLATAFORMAS / RESTAURANTES DISPONIBLES ───────────────────────
# clave interna -> (texto a mostrar, texto de la comisión que se aplica)
# Edita esta lista cuando quieras añadir, quitar o cambiar precios.
PLATAFORMAS = {
    "telepizza":   ("Telepizza",         "Comisión: 5€"),
    "papajohns":   ("Papa John's",       "Comisión: 5€"),
    "kfc":         ("KFC",               "Comisión: 7€"),
    "uber_glovo":  ("Uber Eats / Glovo",  "Comisión: 40% del carrito"),
}

# ─── PRODUCTOS: ROPA ───────────────────────────────────────────────
# clave -> (nombre, precio en €)
ROPA = {
    "camisetas": ("Camisetas", 5.0),
    "pantalones": ("Pantalones", 8.0),
}

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
XP_POR_EURO = 2

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
ADMIN_IDS = {123456789}  # ← reemplaza este número por tu ID real

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

# ─── TECLADOS ─────────────────────────────────────────────────────
def menu_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍔 Pedidos a domicilio", callback_data="cat_pedidos")],
        [InlineKeyboardButton("👕 Ropa", callback_data="cat_ropa")],
        [InlineKeyboardButton("👖 Pantalones", callback_data="cat_pantalones")],
        [InlineKeyboardButton("💰 Saldo", callback_data="cat_saldo")],
        [InlineKeyboardButton("🏹 Target", callback_data="cat_target")],
        [InlineKeyboardButton("🎯 Referidos", callback_data="cat_referidos")],
        [InlineKeyboardButton("🆘 Soporte", url=f"https://t.me/{CONTACTO_ADMIN}")],
    ])

def teclado_referidos_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Mi progreso", callback_data="ref_progreso"),
         InlineKeyboardButton("🔗 Mi enlace", callback_data="ref_enlace")],
        [InlineKeyboardButton("✅ Verificar suscripción", callback_data="ref_verificar")],
        [InlineKeyboardButton("🏅 Ranking TOP", callback_data="ref_ranking")],
        [InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")],
    ])

def teclado_pedidos_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Hacer un pedido", callback_data="nuevo_pedido")],
        [InlineKeyboardButton("📦 Ver mi pedido", callback_data="ver_estado")],
        [InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")],
    ])

def teclado_ropa_menu():
    filas = [
        [InlineKeyboardButton(f"{nombre} — {precio:.0f}€", callback_data=f"ropa_{clave}")]
        for clave, (nombre, precio) in ROPA.items()
    ]
    filas.append([InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")])
    return InlineKeyboardMarkup(filas)

def teclado_saldo_menu():
    filas = [
        [InlineKeyboardButton(texto, callback_data=f"{clave}")]
        for clave, (texto, _precio) in SALDO.items()
    ]
    filas.append([InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")])
    return InlineKeyboardMarkup(filas)

def volver_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")]])

def cancelar_conv_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar pedido", callback_data="cancelar_conv")]])

def teclado_restaurantes():
    filas = [
        [InlineKeyboardButton(f"{nombre} — {comision}", callback_data=f"restaurante_{clave}")]
        for clave, (nombre, comision) in PLATAFORMAS.items()
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
TEXTO_MENU = (
    "🛍️ *¡Bienvenido a Zero Shop!*\n\n"
    "Bot para compras automáticas — por si quieres ser de los primeros en ser atendido.\n\n"
    "Elige una categoría:"
)

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
            bonus_msg = "\n\n👋 ¡Te han invitado! Ve a 🎯 *Referidos* → únete al canal y pulsa *Verificar suscripción* para que tu amigo reciba su XP."

    save_db(db)
    await update.message.reply_text(TEXTO_MENU + bonus_msg, parse_mode="Markdown", reply_markup=menu_principal())

async def volver_al_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(TEXTO_MENU, parse_mode="Markdown", reply_markup=menu_principal())

# ─── CATEGORÍA: REFERIDOS ──────────────────────────────────────────
async def categoria_referidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🎯 *Referidos*\n\n"
        "Invita a tus amigos, sube de nivel y desbloquea descuentos en Zero Shop.\n\n"
        "Elige una opción:",
        parse_mode="Markdown",
        reply_markup=teclado_referidos_menu()
    )

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

async def otorgar_xp(context, referrer_id, referrer_data, premios):
    nivel_antes, *_ = calcular_nivel(referrer_data["xp"])
    referrer_data["xp"] += XP_POR_REFERIDO
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
    u["xp"] += xp_ganada
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
def crear_compra(db, comprador_id, username, producto, precio_eur):
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

async def capturar_hash_pago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captura el siguiente mensaje de texto del usuario como el hash de la transacción,
    solo si estaba pendiente de enviarlo (fuera de esto, no hace nada)."""
    compra_id = context.user_data.get("compra_esperando_hash")
    if not compra_id:
        return

    hash_tx = update.message.text.strip()
    db = load_db()
    compra = db["compras"].get(compra_id)
    if not compra:
        context.user_data.pop("compra_esperando_hash", None)
        return

    compra["hash"] = hash_tx
    compra["estado"] = "pendiente_verificacion"
    save_db(db)
    context.user_data.pop("compra_esperando_hash", None)

    await update.message.reply_text(
        "✅ Hash recibido. Estamos verificando el pago, te avisaremos en cuanto se confirme.",
        reply_markup=volver_menu_keyboard()
    )

    try:
        await context.bot.send_message(
            chat_id=GRUPO_ADMIN_ID,
            text=(
                f"💰 *PAGO PENDIENTE DE VERIFICAR — Compra #{compra_id}*\n\n"
                f"Producto: {compra['producto']}\n"
                f"Importe: {compra['precio_eur']:.2f}€ ≈ {compra.get('cantidad_crypto', 0):.8f} {compra['metodo'].upper()}\n"
                f"Comprador: @{compra['username']} (ID `{compra['comprador_id']}`)\n"
                f"Hash: `{hash_tx}`"
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirmar pago recibido", callback_data=f"compraconfirmar_{compra_id}")]])
        )
    except Exception as e:
        logging.error(f"No se pudo notificar la compra #{compra_id} al grupo: {e}")

async def compra_confirmar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """El admin confirma en el grupo que el pago (cripto o transferencia) ha llegado."""
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

    compra["estado"] = "confirmada"
    comprador_id = str(compra["comprador_id"])
    comprador = get_usuario(db, comprador_id)
    await otorgar_xp_compra(context, comprador_id, comprador, compra["precio_eur"], get_premios(db))
    save_db(db)

    await q.edit_message_text(q.message.text_markdown + "\n\n✅ *PAGO CONFIRMADO*", parse_mode="Markdown")

    try:
        await context.bot.send_message(
            chat_id=compra["comprador_id"],
            text=f"✅ Hemos confirmado tu pago de la compra *#{compra_id}* ({compra['producto']}). ¡Gracias!",
            parse_mode="Markdown"
        )
    except Exception:
        pass

async def ref_progreso_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    uid = str(q.from_user.id)
    u = get_usuario(db, uid)
    bot_user = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_user.username}?start={uid}"

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
📊 *TU PROGRESO*

👤 Usuario: @{u.get('username','?')}
🏅 Nivel: *{nivel} / {NIVEL_MAX}*
⭐ XP total: *{u['xp']}*
👥 Referidos verificados: *{refs}*

{bar} {pct}%
{progreso_txt}

🏁 *Premios:*
{premios_txt}

🔗 Tu enlace:
`{ref_link}`
"""
    await q.edit_message_text(
        texto, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="cat_referidos")]])
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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="cat_referidos")]])
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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="cat_referidos")]])
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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="cat_referidos")]])
    )

# ─── COMANDOS DE ADMINISTRACIÓN (gestión de premios y entregas) ───
def es_admin(user_id) -> bool:
    return user_id in ADMIN_IDS

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

# ─── CATEGORÍA: PEDIDOS A DOMICILIO ───────────────────────────────
async def categoria_pedidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🍔 *Pedidos a domicilio*\n\nElige una opción:",
        parse_mode="Markdown",
        reply_markup=teclado_pedidos_menu()
    )

# ─── CATEGORÍA: ROPA ───────────────────────────────────────────────
async def categoria_ropa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "👕 *Ropa*\n\nElige una prenda:",
        parse_mode="Markdown",
        reply_markup=teclado_ropa_menu()
    )

async def ver_producto_ropa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    clave = q.data.split("_", 1)[1]
    nombre, precio = ROPA[clave]
    db = load_db()
    compra_id = crear_compra(db, q.from_user.id, q.from_user.username or q.from_user.first_name, nombre, precio)
    await iniciar_pago(q, context, compra_id, precio, nombre)

# ─── CATEGORÍA: PANTALONES ──────────────────────────────────────────
# 8 opciones numeradas, todas al mismo precio (da igual cuál se elija).
NUM_PANTALONES = 8
PRECIO_PANTALONES = 4.99

def teclado_pantalones_menu():
    filas = [
        [InlineKeyboardButton(f"{i} — {PRECIO_PANTALONES:.2f}€", callback_data=f"pantalon_{i}")]
        for i in range(1, NUM_PANTALONES + 1)
    ]
    filas.append([InlineKeyboardButton("🔙 Menú principal", callback_data="menu_principal")])
    return InlineKeyboardMarkup(filas)

async def categoria_pantalones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        f"👖 *Pantalones*\n\nTodas las opciones al mismo precio: *{PRECIO_PANTALONES:.2f}€*.\n\nElige una:",
        parse_mode="Markdown",
        reply_markup=teclado_pantalones_menu()
    )

async def ver_pantalon_numero(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    numero = q.data.split("_", 1)[1]
    db = load_db()
    compra_id = crear_compra(
        db, q.from_user.id, q.from_user.username or q.from_user.first_name,
        f"Pantalones (opción {numero})", PRECIO_PANTALONES
    )
    await iniciar_pago(q, context, compra_id, PRECIO_PANTALONES, f"Pantalones (opción {numero})")

# ─── CATEGORÍA: SALDO ──────────────────────────────────────────────
async def categoria_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "💰 *Saldo*\n\nElige la cantidad:",
        parse_mode="Markdown",
        reply_markup=teclado_saldo_menu()
    )

async def ver_producto_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    texto_opcion, precio = SALDO[q.data]

    if precio is None:
        await q.edit_message_text(
            f"💰 *{texto_opcion}*\n\nEsta opción no tiene coste asociado. Si tienes dudas, contacta con soporte.",
            parse_mode="Markdown",
            reply_markup=volver_menu_keyboard()
        )
        return

    db = load_db()
    compra_id = crear_compra(db, q.from_user.id, q.from_user.username or q.from_user.first_name, f"Saldo {texto_opcion}", precio)
    await iniciar_pago(q, context, compra_id, precio, f"Saldo {texto_opcion}")

# ─── CATEGORÍA: TARGET ──────────────────────────────────────────────
PRECIO_TARGET = 4.99

async def categoria_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = load_db()
    compra_id = crear_compra(db, q.from_user.id, q.from_user.username or q.from_user.first_name, "Target", PRECIO_TARGET)
    await iniciar_pago(q, context, compra_id, PRECIO_TARGET, "Target")

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
    await q.edit_message_text(
        "🍽️ *¿Dónde quieres pedir?*",
        parse_mode="Markdown",
        reply_markup=teclado_restaurantes()
    )
    return RESTAURANTE

async def seleccionar_restaurante_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    clave = q.data.split("_", 1)[1]

    if clave == "personalizado":
        context.user_data["restaurante_clave"] = "personalizado"
        context.user_data["restaurante_nombre"] = "Pedido personalizado"
        context.user_data["comision_texto"] = "A consultar"
        await q.edit_message_text(
            "✏️ *Pedido personalizado*\n\n"
            "Cuéntanos qué quieres pedir y dónde (restaurante, tienda, plataforma...).",
            parse_mode="Markdown",
            reply_markup=cancelar_conv_keyboard()
        )
        return PEDIDO

    nombre, comision = PLATAFORMAS[clave]
    context.user_data["restaurante_clave"] = clave
    context.user_data["restaurante_nombre"] = nombre
    context.user_data["comision_texto"] = comision

    await q.edit_message_text(
        f"🍔 *Pedido en {nombre}* ({comision})\n\n"
        f"Escribe qué quieres pedir (ej. _2 hamburguesas, 1 patatas grandes, 1 refresco_).",
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
    return await pedir_precio_pedido(update.message, context)

async def saltar_comentarios_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["comentarios"] = "-"
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
    await q.edit_message_text(TEXTO_MENU, parse_mode="Markdown", reply_markup=menu_principal())
    return ConversationHandler.END

# ─── CONFIRMACIÓN Y ENVÍO AL GRUPO DE ADMINS ──────────────────────
def texto_para_admin(numero, d):
    return f"""
🔔 *PEDIDO #{numero}*

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
        fallbacks=[CallbackQueryHandler(cancelar_conv_callback, pattern="^cancelar_conv$")],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(categoria_pedidos, pattern="^cat_pedidos$"))
    app.add_handler(CallbackQueryHandler(categoria_ropa, pattern="^cat_ropa$"))
    app.add_handler(CallbackQueryHandler(ver_producto_ropa, pattern="^ropa_"))
    app.add_handler(CallbackQueryHandler(categoria_pantalones, pattern="^cat_pantalones$"))
    app.add_handler(CallbackQueryHandler(ver_pantalon_numero, pattern="^pantalon_"))
    app.add_handler(CallbackQueryHandler(categoria_saldo, pattern="^cat_saldo$"))
    app.add_handler(CallbackQueryHandler(ver_producto_saldo, pattern="^saldo_"))
    app.add_handler(CallbackQueryHandler(categoria_target, pattern="^cat_target$"))
    app.add_handler(CallbackQueryHandler(categoria_referidos, pattern="^cat_referidos$"))
    app.add_handler(CallbackQueryHandler(ref_progreso_callback, pattern="^ref_progreso$"))
    app.add_handler(CallbackQueryHandler(ref_enlace_callback, pattern="^ref_enlace$"))
    app.add_handler(CallbackQueryHandler(ref_verificar_callback, pattern="^ref_verificar$"))
    app.add_handler(CallbackQueryHandler(ref_ranking_callback, pattern="^ref_ranking$"))
    app.add_handler(CommandHandler("setpremio", cmd_setpremio))
    app.add_handler(CommandHandler("quitarpremio", cmd_quitarpremio))
    app.add_handler(CommandHandler("premios", cmd_premios))
    app.add_handler(CommandHandler("pendientes", cmd_pendientes))
    app.add_handler(CommandHandler("entregar", cmd_entregar))
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_hash_pago))

    print("🛍️ Bot Zero Shop arrancado...")
    app.run_polling()

if __name__ == "__main__":
    main()
