"""
Módulo de alertas a Telegram con botones de acción
Envía notificaciones al grupo de sistemas con opciones: IR TÉCNICO / RESUELTO
"""

import os
import html
import logging
import requests
from dotenv import load_dotenv
from logging_config import get_logger

load_dotenv()
log = get_logger(__name__)


def _esc(texto):
    """Escapa caracteres especiales para parse_mode HTML de Telegram."""
    return html.escape(str(texto if texto is not None else ''))

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')


def enviar_alerta(tipo, radicado, nombre_cliente, cedula, telefono, detalle):
    """
    Enviar alerta a Telegram con botones de acción

    Args:
        tipo: AFILIACION, COBERTURA, EQUIPO_DAÑADO, PQR, URGENTE, SISTEMAS
        radicado: VES-XXXX
        nombre_cliente: Nombre del cliente
        cedula: Cédula del cliente
        telefono: Teléfono del cliente
        detalle: Descripción del caso
    """

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("⚠️ Telegram no configurado")
        return False

    emojis = {
        # Escalamientos
        'AFILIACION':    '🔴',
        'COBERTURA':     '🟡',
        'EQUIPO_DAÑADO': '🟠',
        'PQR':           '🔵',
        'URGENTE':       '⚪',
        'SISTEMAS':      '⚠️',
        # Reclamos / órdenes de revisión de servicio
        'INTERNET':      '📶',
        'TV':            '📺',
        'AMBOS':         '📡',
        'CORTE':         '✂️',
        'CAMBIO_WIFI':   '🔧',
        'CAMBIO_PLAN':   '🔄',
        'SUSPENSION':    '⏸️',
        'TRASLADO':      '📦',
        'CAMBIO_TITULAR':'📝',
    }

    emoji = emojis.get(tipo, '🚨')
    from datetime import datetime
    fecha_actual = datetime.now().strftime('%Y-%m-%d')

    mensaje = (
        f"{emoji} <b>RECLAMO Nº {_esc(radicado)}</b>\n"
        f"Creado el: {fecha_actual}\n"
        f"{'─' * 40}\n\n"
        f"<b>👤 CLIENTE</b>\n"
        f"Nombre: {_esc(nombre_cliente)}\n"
        f"Documento: {_esc(cedula)}\n"
        f"Celular: {_esc(telefono)}\n"
        f"Dirección: [Pendiente CRM]\n"
        f"Barrio: [Pendiente CRM]\n\n"
        f"<b>📦 CASO</b>\n"
        f"Estado: Pendiente Revisión\n"
        f"Tipo: {_esc(tipo)}\n"
        f"Servicios: [Pendiente CRM]\n\n"
        f"<b>⚠️ DESCRIPCIÓN</b>\n"
        f"{_esc(detalle)}\n\n"
        f"{'─' * 40}\n"
        f"<i>¿Qué acción tomar?</i>"
    )

    # Botones inline — callback_data solo lleva: ACCION|RADICADO|TELEFONO
    # Los datos del cliente se recuperan desde la BD usando el radicado
    callback_tecnico = f"TECNICO|{radicado}|{telefono}"
    callback_resuelto = f"RESUELTO|{radicado}|{telefono}"

    inline_keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "🔧 ENVIAR TÉCNICO",
                    "callback_data": callback_tecnico
                }
            ],
            [
                {
                    "text": "✅ MARCAR RESUELTO",
                    "callback_data": callback_resuelto
                }
            ]
        ]
    }

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    data = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': mensaje,
        'parse_mode': 'HTML',
        'reply_markup': inline_keyboard
    }

    try:
        response = requests.post(url, json=data, timeout=10)

        if response.status_code == 200:
            log.info(f"✅ Alerta enviada a Telegram: {radicado}")
            return True
        else:
            log.error(f"❌ Error Telegram: {response.text}")
            return False

    except Exception as e:
        log.error(f"❌ Error enviando alerta a Telegram: {str(e)}")
        return False


def editar_mensaje_accion(chat_id, message_id, radicado, accion, nombre_cliente='Cliente', cedula='Pendiente', telefono=''):
    """
    Edita el mensaje en Telegram mostrando toda la información del reclamo + la acción ejecutada.
    Preserva todos los datos del cliente para mantener un registro completo.
    """
    if not TELEGRAM_BOT_TOKEN:
        return False

    from datetime import datetime
    fecha_actual = datetime.now().strftime('%Y-%m-%d')

    # Reconstruir el mensaje completo con todos los datos del cliente
    mensaje_completo = (
        f"📋 <b>RECLAMO Nº {_esc(radicado)}</b>\n"
        f"Creado el: {fecha_actual}\n"
        f"{'─' * 40}\n\n"
        f"<b>👤 CLIENTE</b>\n"
        f"Nombre: {_esc(nombre_cliente)}\n"
        f"Documento: {_esc(cedula)}\n"
        f"Celular: {_esc(telefono)}\n"
        f"Dirección: [Pendiente CRM]\n"
        f"Barrio: [Pendiente CRM]\n\n"
    )

    # Agregar la acción ejecutada
    acciones = {
        'TECNICO': (
            f"<b>✅ ACCIÓN EJECUTADA</b>\n"
            f"🔧 Técnico asignado\n"
            f"Se notificó al cliente que un técnico se desplazará a su vivienda.\n"
            f"Aguardando confirmación de visita."
        ),
        'RESUELTO': (
            f"<b>✅ ACCIÓN EJECUTADA</b>\n"
            f"✅ Marcado como resuelto\n"
            f"Se envió mensaje al cliente para verificar si el servicio está funcionando.\n"
            f"Aguardando confirmación del cliente..."
        )
    }

    texto_accion = acciones.get(accion, "<b>✅ ACCIÓN EJECUTADA</b>")
    texto_final = mensaje_completo + f"{'─' * 40}\n\n{texto_accion}"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"

    data = {
        'chat_id': chat_id,
        'message_id': message_id,
        'text': texto_final,
        'parse_mode': 'HTML'
    }

    try:
        response = requests.post(url, json=data, timeout=10)
        return response.status_code == 200
    except Exception as e:
        log.error(f"❌ Error editando mensaje en Telegram: {str(e)}")
        return False


def responder_callback(callback_query_id):
    """Confirma a Telegram que el callback fue procesado (quita el spinner)"""
    if not TELEGRAM_BOT_TOKEN:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={'callback_query_id': callback_query_id}, timeout=5)
    except Exception:
        pass


if __name__ == "__main__":
    print("Enviando alerta de prueba al grupo...")
    enviar_alerta(
        tipo='SISTEMAS',
        radicado='VES-TEST',
        nombre_cliente='Luis López',
        telefono='+573001234567',
        detalle='Internet lento - Bombillo rojo - PRUEBA'
    )
