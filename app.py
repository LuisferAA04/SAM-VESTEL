"""
Aplicación Flask Principal - SAM Vestel
Servidor web con API REST, panel de administración y webhook de Telegram
"""

import os
import sys

# Forzar UTF-8 en la salida estándar: en Windows la consola usa cp1252 y los
# print() con emojis (✅, 🤖, etc.) lanzan UnicodeEncodeError y tumban el arranque.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except Exception:
        pass

from functools import wraps
from flask import Flask, render_template, request, jsonify, abort, Response
from dotenv import load_dotenv
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from agent_sam import AgenteSAM
from database import Database, insertar_datos_prueba
from logging_config import setup_logging, get_logger
from datetime import datetime
import tempfile
import requests as http_requests
import logging

# Configurar logging global
setup_logging()
log = get_logger(__name__)

# Cargar variables de entorno
load_dotenv()

# Inicializar Flask
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

# Inicializar componentes
api_key = os.getenv('OPENAI_API_KEY')
model   = os.getenv('OPENAI_MODEL', 'gpt-4o')
agente  = AgenteSAM(api_key, model)
db      = Database()

# Insertar datos de prueba si la BD está vacía
insertar_datos_prueba()

log.info("=" * 60)
log.info("🤖 SAM - VESTEL | Sistema de Atención al Cliente")
log.info("=" * 60)


# ═══════════════════════════════════════════════════════════
# SEGURIDAD DE WEBHOOKS
# ═══════════════════════════════════════════════════════════

_twilio_validator = RequestValidator(os.getenv('TWILIO_AUTH_TOKEN', ''))
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', '').rstrip('/')
TELEGRAM_WEBHOOK_SECRET = os.getenv('TELEGRAM_WEBHOOK_SECRET')


def validar_twilio(func):
    """
    Decorador: rechaza peticiones cuya firma X-Twilio-Signature no sea válida.
    Evita que terceros suplanten a clientes en el webhook de WhatsApp.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        auth_token = os.getenv('TWILIO_AUTH_TOKEN')
        if not auth_token:
            log.error("⚠️ TWILIO_AUTH_TOKEN no configurado: no se puede validar la firma del webhook")
            abort(503)

        # URL pública real (Twilio firma sobre la URL exacta que invocó).
        # Detrás de un proxy, request.url puede traer http interno: usamos PUBLIC_BASE_URL si está.
        if PUBLIC_BASE_URL:
            url = PUBLIC_BASE_URL + request.path
        else:
            url = request.url

        signature = request.headers.get('X-Twilio-Signature', '')
        if not _twilio_validator.validate(url, request.form.to_dict(), signature):
            log.warning("❌ Firma de Twilio inválida — petición rechazada")
            abort(403)

        return func(*args, **kwargs)
    return wrapper


# ═══════════════════════════════════════════════════════════
# RUTAS WEB
# ═══════════════════════════════════════════════════════════

@app.route('/')
def index():
    """Página principal - Dashboard WhatsApp"""
    return render_template('dashboard.html')


# ═══════════════════════════════════════════════════════════
# API REST
# ═══════════════════════════════════════════════════════════

@app.route('/api/chat', methods=['POST'])
def api_chat():
    """
    Procesar mensaje del cliente

    Body JSON:
    {
        "telefono": "+573001234567",
        "mensaje": "Hola, tengo un problema"
    }
    """
    try:
        data = request.get_json()

        if not data or 'telefono' not in data or 'mensaje' not in data:
            return jsonify({'error': 'Faltan campos: telefono y mensaje'}), 400

        telefono = data['telefono']
        mensaje  = data['mensaje']

        resultado = agente.procesar_mensaje(telefono, mensaje)

        return jsonify({
            'exito':            resultado['exito'],
            'respuesta':        resultado.get('respuesta_limpia', resultado['respuesta']),
            'radicado':         resultado.get('radicado'),
            'escalamiento':     resultado.get('escalamiento', False),
            'tipo_escalamiento':resultado.get('tipo_escalamiento'),
            'tokens_usados':    resultado.get('tokens_usados', 0),
            'timestamp':        datetime.now().isoformat()
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/conversaciones', methods=['GET'])
def api_conversaciones():
    """Obtener lista de conversaciones activas"""
    try:
        conversaciones = db.obtener_conversaciones_activas(limite=50)
        return jsonify({'total': len(conversaciones), 'conversaciones': conversaciones})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/conversacion/<int:conversacion_id>/mensajes', methods=['GET'])
def api_mensajes(conversacion_id):
    """Obtener mensajes de una conversación"""
    try:
        mensajes = db.obtener_mensajes(conversacion_id)
        return jsonify({'conversacion_id': conversacion_id, 'total': len(mensajes), 'mensajes': mensajes})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/conversacion/<int:conversacion_id>/intervenir', methods=['POST'])
def api_intervenir(conversacion_id):
    """Cambiar modo de conversación a manual"""
    try:
        db.actualizar_conversacion(conversacion_id, modo='manual')
        return jsonify({'exito': True, 'mensaje': 'Modo manual activado'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/conversacion/<int:conversacion_id>/automatico', methods=['POST'])
def api_automatico(conversacion_id):
    """Cambiar modo de conversación a automático"""
    try:
        db.actualizar_conversacion(conversacion_id, modo='automatico')
        return jsonify({'exito': True, 'mensaje': 'Modo automático activado'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/conversacion/<int:conversacion_id>/enviar', methods=['POST'])
def api_enviar_manual(conversacion_id):
    """Enviar un mensaje manual (modo agente) al cliente vía WhatsApp"""
    try:
        data = request.get_json()
        mensaje = (data or {}).get('mensaje', '').strip()
        if not mensaje:
            return jsonify({'error': 'mensaje vacío'}), 400

        conv = db.obtener_conversacion_por_id(conversacion_id)
        if not conv:
            return jsonify({'error': 'conversación no encontrada'}), 404

        enviado = enviar_whatsapp_twilio(conv['telefono'], mensaje)
        db.guardar_mensaje(
            conversacion_id=conversacion_id,
            tipo='texto',
            contenido=mensaje,
            remitente='agente'
        )
        return jsonify({'exito': enviado, 'enviado_whatsapp': enviado})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/casos', methods=['GET'])
def api_casos():
    """Obtener casos recientes"""
    try:
        casos = db.obtener_casos_recientes(limite=20)
        return jsonify({'total': len(casos), 'casos': casos})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/alertas', methods=['GET'])
def api_alertas():
    """Obtener alertas activas"""
    try:
        alertas = db.obtener_alertas_activas()
        return jsonify({'total': len(alertas), 'alertas': alertas})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/estadisticas', methods=['GET'])
def api_estadisticas():
    """Obtener estadísticas día/semana/mes"""
    try:
        stats = db.obtener_estadisticas()
        return jsonify(stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/usuario/<cedula>', methods=['GET'])
def api_usuario(cedula):
    """Buscar usuario por cédula"""
    try:
        usuario = db.buscar_usuario(cedula)
        if not usuario:
            return jsonify({'encontrado': False, 'mensaje': 'Usuario no encontrado'}), 404
        return jsonify({'encontrado': True, 'usuario': usuario})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/health', methods=['GET'])
def health():
    """Health check del servicio"""
    return jsonify({
        'status':    'ok',
        'servicio':  'SAM - Vestel',
        'version':   '1.1.0',
        'timestamp': datetime.now().isoformat()
    })


# ═══════════════════════════════════════════════════════════
# WHATSAPP (Twilio)
# ═══════════════════════════════════════════════════════════

def enviar_whatsapp_twilio(telefono, mensaje):
    """
    Enviar mensaje proactivo a WhatsApp vía Twilio REST API
    (usado cuando sistemas presiona un botón en Telegram)
    """
    from twilio.rest import Client

    account_sid = os.getenv('TWILIO_ACCOUNT_SID')
    auth_token  = os.getenv('TWILIO_AUTH_TOKEN')
    from_number = os.getenv('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886')

    if not account_sid or not auth_token:
        log.error("⚠️ Twilio no configurado en .env")
        return False

    try:
        client = Client(account_sid, auth_token)
        client.messages.create(
            body=mensaje,
            from_=from_number,
            to=f'whatsapp:{telefono}'
        )
        log.info(f"✅ Mensaje enviado a {telefono}: {mensaje}")
        return True
    except Exception as e:
        log.error(f"❌ Error Twilio: {str(e)}")
        return False


def _transcribir_audio_twilio(media_url):
    """
    Descarga un audio desde Twilio y lo transcribe con OpenAI Whisper.
    Retorna el texto transcrito o None si falla.
    """
    account_sid = os.getenv('TWILIO_ACCOUNT_SID')
    auth_token = os.getenv('TWILIO_AUTH_TOKEN')

    try:
        resp = http_requests.get(media_url, auth=(account_sid, auth_token), timeout=30)
        if resp.status_code != 200:
            log.error(f"❌ No se pudo descargar audio: HTTP {resp.status_code}")
            return None

        content_type = resp.headers.get('Content-Type', '')
        ext = '.ogg'
        if 'mp3' in content_type or 'mpeg' in content_type:
            ext = '.mp3'
        elif 'mp4' in content_type or 'm4a' in content_type:
            ext = '.m4a'
        elif 'wav' in content_type:
            ext = '.wav'
        elif 'webm' in content_type:
            ext = '.webm'

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name

        from openai import OpenAI
        client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        with open(tmp_path, 'rb') as audio_file:
            transcripcion = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="es",
            )

        os.unlink(tmp_path)
        texto = transcripcion.text.strip()
        log.info(f"🎙️ Audio transcrito: {texto}")
        return texto

    except Exception as e:
        log.error(f"❌ Error transcribiendo audio: {e}")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return None


@app.route('/api/whatsapp', methods=['POST'])
@validar_twilio
def api_whatsapp():
    """Recibir mensajes de WhatsApp vía Twilio webhook"""
    try:
        from_number  = request.form.get('From', '').replace('whatsapp:', '')
        message_body = request.form.get('Body', '')

        # Detectar si el cliente envió un audio/nota de voz
        num_media = int(request.form.get('NumMedia', 0))
        audio_transcrito = None
        if num_media > 0:
            for i in range(num_media):
                media_type = request.form.get(f'MediaContentType{i}', '')
                media_url = request.form.get(f'MediaUrl{i}', '')
                if media_type.startswith('audio/') and media_url:
                    audio_transcrito = _transcribir_audio_twilio(media_url)
                    break

        if audio_transcrito:
            message_body = audio_transcrito
        elif not message_body and num_media > 0:
            media_type = request.form.get('MediaContentType0', '')
            if media_type.startswith('image/'):
                return _twiml("Recibí tu imagen. Por el momento solo puedo atender mensajes de texto y notas de voz. ¿En qué puedo ayudarte? 😊")
            elif media_type.startswith('video/'):
                return _twiml("Recibí tu video. Por el momento solo puedo atender mensajes de texto y notas de voz. ¿En qué puedo ayudarte? 😊")
            else:
                return _twiml("Recibí tu archivo. Por el momento solo puedo atender mensajes de texto y notas de voz. ¿En qué puedo ayudarte? 😊")

        if not from_number or not message_body:
            return '', 400

        tipo_msg = "🎙️ Audio" if audio_transcrito else "📱 Texto"
        log.info(f"{tipo_msg} WhatsApp de {from_number}: {message_body}")

        # Verificar si el cliente está esperando confirmación de "resuelto"
        conv = db.obtener_conversacion(from_number)
        if conv and conv.get('modo') == 'esperando_confirmacion':
            return _manejar_confirmacion_resuelto(from_number, message_body, conv)

        # Si viene de audio, agregar contexto para que el modelo sepa
        mensaje_para_sam = message_body
        if audio_transcrito:
            mensaje_para_sam = f"[El cliente envió una nota de voz. Transcripción: \"{message_body}\"]"

        resultado = agente.procesar_mensaje(from_number, mensaje_para_sam)
        respuesta_texto = resultado.get('respuesta_limpia', resultado['respuesta'])

        log.info(f"🤖 SAM responde: {respuesta_texto}")
        return _twiml(respuesta_texto)

    except Exception as e:
        log.error(f"❌ Error WhatsApp: {str(e)}", exc_info=True)
        return '', 500


def _twiml(mensaje):
    """Construye una respuesta TwiML escapando el contenido de forma segura."""
    resp = MessagingResponse()
    resp.message(mensaje)
    return Response(str(resp), mimetype='text/xml')


def _manejar_confirmacion_resuelto(telefono, mensaje, conv):
    """
    El cliente respondió después de que sistemas marcó 'RESUELTO'.
    Solo procesa si confirma explícitamente SÍ o NO.
    Si dice que va a revisar, espera confirmación real.
    """
    mensaje_lower = mensaje.lower().strip()

    # Palabras positivas = servicio funciona
    palabras_positivas = ['si', 'sí', 'yes', 'ok', 'okay', 'funciona', 'funciono',
                         'perfecto', 'excelente', 'bien', 'bien!', 'ya', 'listo',
                         'claro', 'claro que si', 'claro que sí', 'que bueno', 'qué bueno']

    # Palabras negativas = servicio NO funciona
    palabras_negativas = ['no', 'nope', 'sigue', 'todavia', 'todavía',
                          'aun', 'aún', 'igual', 'mismo', 'problema',
                          'falla', 'fallo', 'malo', 'mal', 'sin', 'nada',
                          'sigue sin', 'no funciona', 'no funciono', 'no sirve']

    # Palabras de revisión = cliente va a verificar, NO responde aún
    palabras_revision = ['momento', 'reviso', 'reviso', 'valido', 'válido', 'verifico',
                        'verificar', 'chequeo', 'chequear', 'revisar', 'déjame ver',
                        'espera', 'espere', 'un momento', 'un segundo', 'un minuto']

    es_positivo = any(p in mensaje_lower for p in palabras_positivas)
    es_negativo = any(p in mensaje_lower for p in palabras_negativas)
    es_revision = any(p in mensaje_lower for p in palabras_revision)

    # Si está diciendo que va a revisar, esperar confirmación real
    if es_revision and not es_positivo and not es_negativo:
        respuesta = (
            "Perfecto, tómate el tiempo que necesites. "
            "Avísame cuando hayas verificado si el servicio funciona correctamente. 👍"
        )
        # Mantener en modo esperando confirmación
        return _twiml(respuesta)

    if es_negativo:
        respuesta = (
            "Entiendo, lamento que el inconveniente persista. "
            "He escalado nuevamente su caso y un técnico se desplazará a su vivienda "
            "para revisar el servicio. En breve le contactaremos para coordinar la visita. "
            "¡Gracias por su paciencia! 🔧"
        )
        # Volver a modo automático y notificar a Telegram
        db.actualizar_conversacion(conv['id'], modo='automatico')

        try:
            from telegram_alerts import enviar_alerta
            enviar_alerta(
                tipo='URGENTE',
                radicado=f"VES-REVISITA",
                nombre_cliente=conv.get('nombre_cliente', 'Cliente'),
                cedula=conv.get('cedula', 'Pendiente'),
                telefono=telefono,
                detalle='Cliente confirmó que el problema NO fue resuelto. Requiere visita técnica.'
            )
        except Exception as e:
            log.warning(f"No se pudo enviar alerta a Telegram: {e}")

    elif es_positivo:
        respuesta = (
            "¡Perfecto! Me alegra que el servicio esté funcionando correctamente. "
            "Si en algún momento necesita ayuda, no dude en escribirnos. "
            "¡Que tenga un excelente día! 😊"
        )
        db.actualizar_conversacion(conv['id'], modo='automatico')

    else:
        # Respuesta por defecto si el mensaje no es claro
        respuesta = (
            "Disculpa, no capté bien tu respuesta. "
            "¿El servicio está funcionando correctamente? (responde 'sí' o 'no')"
        )
        # Mantener esperando confirmación
        return _twiml(respuesta)

    return _twiml(respuesta)


# ═══════════════════════════════════════════════════════════
# TELEGRAM WEBHOOK (callbacks de botones)
# ═══════════════════════════════════════════════════════════

@app.route('/api/telegram-webhook', methods=['POST'])
def telegram_webhook():
    """
    Recibe callbacks cuando sistemas presiona IR TÉCNICO o RESUELTO en Telegram
    """
    try:
        # Validar que el callback realmente venga de Telegram (token secreto del webhook).
        # Telegram envía este header cuando registras el webhook con secret_token.
        if TELEGRAM_WEBHOOK_SECRET:
            recibido = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
            if recibido != TELEGRAM_WEBHOOK_SECRET:
                log.warning("❌ Token secreto de Telegram inválido — petición rechazada")
                abort(403)

        data = request.get_json()

        if not data:
            return jsonify({'ok': True})

        # Callback de botón inline
        if 'callback_query' in data:
            callback      = data['callback_query']
            callback_id   = callback['id']
            callback_data = callback.get('data', '')
            chat_id       = callback['message']['chat']['id']
            message_id    = callback['message']['message_id']

            from telegram_alerts import editar_mensaje_accion, responder_callback

            # Formato callback_data: "ACCION|RADICADO|TELEFONO"
            partes = callback_data.split('|')
            if len(partes) != 3:
                responder_callback(callback_id)
                return jsonify({'ok': True})

            accion = partes[0]
            radicado = partes[1]
            telefono = partes[2]

            # Recuperar datos del cliente desde la BD usando el radicado
            try:
                caso = db.obtener_caso(radicado)
                cedula = caso.get('cedula', 'Pendiente') if caso else 'Pendiente'
                nombre_cliente = caso.get('nombre_cliente', 'Cliente') if caso else 'Cliente'
            except Exception:
                cedula = 'Pendiente'
                nombre_cliente = 'Cliente'

            # Confirmar a Telegram que recibimos el callback
            responder_callback(callback_id)

            if accion == 'TECNICO':
                mensaje_cliente = (
                    "Se verificó desde el área de sistemas y no se logró dar solución. "
                    "Es necesario que pase el técnico por la vivienda. "
                    "En breve le contactaremos para coordinar la visita. 🔧"
                )
                enviar_whatsapp_twilio(telefono, mensaje_cliente)
                editar_mensaje_accion(chat_id, message_id, radicado, 'TECNICO', nombre_cliente, cedula, telefono)

                # Actualizar estado del caso
                try:
                    conv = db.obtener_conversacion(telefono)
                    if conv:
                        db.actualizar_conversacion(conv['id'], modo='automatico')
                except Exception:
                    pass

            elif accion == 'RESUELTO':
                mensaje_cliente = (
                    "Desde el área de sistemas me indican que se logró dar solución al servicio. "
                    "¿Puede verificar por favor? ✅"
                )
                enviar_whatsapp_twilio(telefono, mensaje_cliente)
                editar_mensaje_accion(chat_id, message_id, radicado, 'RESUELTO', nombre_cliente, cedula, telefono)

                # Poner conversación en modo espera de confirmación
                try:
                    conv = db.obtener_conversacion(telefono)
                    if conv:
                        db.actualizar_conversacion(conv['id'], modo='esperando_confirmacion')
                except Exception:
                    pass

        return jsonify({'ok': True})

    except Exception as e:
        log.error(f"❌ Error webhook Telegram: {str(e)}", exc_info=True)
        return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════
# INICIAR SERVIDOR
# ═══════════════════════════════════════════════════════════

if __name__ == '__main__':
    port  = int(os.getenv('FLASK_PORT', 5000))
    host  = os.getenv('FLASK_HOST', '0.0.0.0')
    debug = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'

    log.info(f"✅ Servidor iniciado en: http://localhost:{port}")
    log.info(f"✅ Dashboard: http://localhost:{port}")
    log.info(f"✅ API REST:  http://localhost:{port}/api/")
    log.info(f"✅ Telegram webhook: http://localhost:{port}/api/telegram-webhook")
    log.info("💡 Presiona Ctrl+C para detener")
    log.info("=" * 60)

    app.run(host=host, port=port, debug=debug)
