"""
Agente SAM - Cerebro conversacional con GPT-4o
Integra el prompt definitivo de Vestel
"""

import os
import re
import json
import random
import logging
import time
from openai import OpenAI
from openai import APIError, RateLimitError, APIConnectionError, APITimeoutError
from datetime import datetime
from database import Database
from logging_config import get_logger

log = get_logger(__name__)


# Herramienta que el modelo invoca para registrar un caso. El servidor genera el
# radicado oficial y se lo devuelve al modelo, de modo que el número que ve el
# cliente SIEMPRE coincide con el guardado en la base de datos.
TOOLS = [{
    "type": "function",
    "function": {
        "name": "registrar_caso",
        "description": (
            "Registra un caso/radicado en el sistema de Vestel y devuelve el número "
            "de radicado oficial. Llama a esta función SOLO cuando ya tienes toda la "
            "información necesaria (y la cédula validada cuando el caso lo requiere). "
            "El radicado que devuelve esta función es el ÚNICO válido: úsalo tal cual "
            "en tu respuesta al cliente. NUNCA inventes un número de radicado."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "servicio": {
                    "type": "string",
                    "description": (
                        "Tipo de caso: internet, tv, ambos, cambio_wifi, cambio_plan, "
                        "suspension, traslado, afiliacion, consulta_cobertura, "
                        "equipo_dañado, pqr, corte, otro"
                    ),
                },
                "descripcion": {
                    "type": "string",
                    "description": "Resumen breve del caso o la falla reportada por el cliente",
                },
                "datos": {
                    "type": "object",
                    "description": (
                        "Datos capturados relevantes al caso (ej. falla, bombillo_rojo, "
                        "wifi_nombre, plan_nuevo, direccion_nueva, fecha_inicio, etc.)"
                    ),
                    "additionalProperties": True,
                },
                "requiere_escalamiento": {
                    "type": "boolean",
                    "description": "True si el caso debe escalarse a un humano",
                },
                "tipo_escalamiento": {
                    "type": "string",
                    "enum": ["AFILIACION", "COBERTURA", "EQUIPO_DAÑADO", "PQR", "URGENTE"],
                    "description": "Tipo de escalamiento (solo si requiere_escalamiento es True)",
                },
            },
            "required": ["servicio", "descripcion"],
        },
    },
}]

# Instrucción que sustituye el viejo formato de marcadores ###DATOS###/###ESCALAMIENTO###.
INSTRUCCION_TOOL = (
    "IMPORTANTE — REGISTRO DE CASOS: cuando debas generar un radicado o derivar un caso al "
    "equipo, NO escribas bloques ###DATOS### ni ###ESCALAMIENTO### en tu respuesta. En su "
    "lugar, llama a la función `registrar_caso`. El sistema te devolverá el número de "
    "radicado oficial; usa ESE número (nunca uno inventado) cuando se lo comuniques al cliente. "
    "Casos que NO requieren cédula previa: afiliacion, consulta_cobertura, pqr.\n"
    "La derivación es INTERNA E INVISIBLE: cuando uses requiere_escalamiento=true, NUNCA le "
    "digas al cliente que 'escalas', 'transfieres', 'derivas' o que lo atenderá 'otra "
    "persona/área'. Háblale como un asesor humano que gestiona su caso: dile con naturalidad "
    "que su solicitud quedó registrada y que un asesor lo contactará para coordinar. Nunca "
    "reveles que eres un sistema o IA. No uses asteriscos (*) para dar formato; usa emojis "
    "cálidos cuando sea apropiado (📶 📺 ⚡ 🎬 💎 🎁 😊)."
)


def _llamar_openai_con_reintentos(func, max_reintentos=3, timeout_inicial=2):
    """
    Wrapper que ejecuta una función OpenAI con reintentos exponenciales.
    Reintenta si falla por rate limit, timeout o error de conexión.

    Args:
        func: callable que hace la llamada a OpenAI (debe lanzar excepción si falla)
        max_reintentos: número máximo de reintentos
        timeout_inicial: segundos iniciales de espera (se duplica cada reintento)

    Returns:
        Resultado de la función si tiene éxito

    Raises:
        APIError: si falla después de agotar reintentos
    """
    for intento in range(max_reintentos):
        try:
            return func()
        except (RateLimitError, APITimeoutError, APIConnectionError) as e:
            if intento < max_reintentos - 1:
                espera = timeout_inicial * (2 ** intento)
                log.warning(
                    f"OpenAI error (intento {intento + 1}/{max_reintentos}): {type(e).__name__}. "
                    f"Reintentando en {espera}s..."
                )
                time.sleep(espera)
            else:
                log.error(
                    f"OpenAI falló después de {max_reintentos} reintentos: {str(e)}"
                )
                raise
        except APIError as e:
            log.error(f"OpenAI error fatal (no reintentable): {str(e)}")
            raise


class AgenteSAM:
    def __init__(self, api_key, model="gpt-4o"):
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.db = Database()

        # MODO_PRUEBAS: si está activo, NO se reconoce a los números recurrentes.
        # Cada saludo reinicia la "sesión" y SAM pide la cédula como si fuera un cliente
        # nuevo. En producción (flag en false) sí recuerda al cliente y lo saluda por nombre.
        self.modo_pruebas = os.getenv('MODO_PRUEBAS', 'false').strip().lower() in ('true', '1', 'si', 'sí')
        if self.modo_pruebas:
            log.warning("🧪 MODO_PRUEBAS activo: no se reconocen números recurrentes (siempre se valida la cédula).")

        prompt_path = os.path.join(os.path.dirname(__file__), 'PROMPT_SAM_DEFINITIVO.md')
        if os.path.exists(prompt_path):
            with open(prompt_path, 'r', encoding='utf-8') as f:
                self.system_prompt = f.read()
        else:
            self.system_prompt = self._get_prompt_compacto()
    
    def _get_prompt_compacto(self):
        return """Eres SAM, asistente virtual de Vestel (telecomunicaciones en Casanare, Colombia).

PERSONALIDAD:
- Profesional, cálida y eficiente
- Español colombiano natural
- Una pregunta a la vez
- Usas el nombre del cliente tras validar cédula
- Saludos según hora: ¡Buen día! (antes 12pm), ¡Buena tarde! (12pm-6pm), ¡Buena noche! (después 6pm)

NUNCA:
- Uses frases de chatbot: "Entendido", "Procesando"
- Pidas más de un dato a la vez
- Preguntes si es el titular
- Generes radicados sin validar cédula

PLANES:
Internet + TV: 100M+TV $77k, 150M+TV $95k, 200M+TV $121k, 250M+TV $142k, 300M+TV $158k
Solo Internet: 100M $66k, 150M $84k, 200M $110k, 250M $131k
Solo TV: $35k/mes
Afiliación: $70k

VALIDACIÓN CÉDULA:
1. Pides: "¿Me regala el número de cédula del titular?"
2. Sistema valida
3. Si existe: "Gracias Sr. [Nombre]..."

RECLAMOS INTERNET:
1. Pedir cédula
2. Validar
3. Especificar falla
4. Preguntar: "¿Al equipo le alumbra algún bombillo rojo?"
5. Generar radicado VES-XXXX

AFILIACIÓN - ESCALAR:
###ESCALAMIENTO###AFILIACION###

COBERTURA - ESCALAR:
###ESCALAMIENTO###COBERTURA###

EQUIPO DAÑADO - ESCALAR:
###ESCALAMIENTO###EQUIPO_DAÑADO###

PQR - ESCALAR:
###ESCALAMIENTO###PQR###

FORMATO SALIDA:
###DATOS###
{"servicio":"tipo","cedula":"123"}
###FIN###
"""
    
    def generar_radicado(self):
        numero = random.randint(1000, 9999)
        radicado = f"VES-{numero}"
        if self.db.obtener_caso(radicado):
            return self.generar_radicado()
        return radicado

    @staticmethod
    def _nombre_corto(nombre_completo):
        """Devuelve 'primer nombre + primer apellido' a partir del nombre completo."""
        if not nombre_completo:
            return None
        partes = nombre_completo.split()
        if len(partes) >= 3:
            # Asume: [primer_nombre, (segundo_nombre...), primer_apellido, ...]
            return f"{partes[0]} {partes[-2]}"
        return nombre_completo

    @staticmethod
    def _es_saludo(mensaje):
        """Detecta si el mensaje es un saludo de inicio (para reiniciar sesión en pruebas)."""
        m = (mensaje or '').strip().lower()
        saludos = ('hola', 'buenas', 'buenos dias', 'buenos días', 'buen dia', 'buen día',
                   'buenas tardes', 'buenas noches', 'hey', 'hi', 'que mas', 'qué más',
                   'saludos', 'buen dia', 'buenass')
        return any(m.startswith(s) for s in saludos)

    @staticmethod
    def _es_despedida(mensaje):
        """Detecta si el mensaje es una despedida (para cerrar sesión en pruebas)."""
        m = (mensaje or '').strip().lower()
        m = re.sub(r'[.,!¡?¿]', '', m).strip()
        despedidas = ('chao', 'chau', 'adios', 'adiós', 'hasta luego', 'nos vemos',
                      'bye', 'gracias por todo', 'eso era todo', 'eso es todo',
                      'no mas gracias', 'no más gracias', 'listo gracias',
                      'ya no necesito nada', 'ya eso es todo', 'muchas gracias',
                      'finalizar', 'terminar', 'fin')
        return any(m.startswith(s) or m == s for s in despedidas)

    def _intentar_validar_cedula(self, conv, mensaje):
        """
        Si el mensaje contiene un número de documento que EXISTE en la base de datos,
        lo persiste en la conversación. Permite también CAMBIAR a una cédula distinta
        si el cliente proporciona otra que sí existe (útil cuando se equivocó o consulta
        por otro titular). Solo sobrescribe con cédulas que son clientes reales.

        Esto ancla la validación al servidor: el radicado no dependerá de que el
        modelo "diga" que validó, sino de un dato real en la BD.
        """
        if not conv:
            return conv

        # Buscar secuencias de 6 a 12 dígitos en el mensaje
        candidatos = re.findall(r'\d{6,12}', mensaje.replace('.', '').replace(',', ''))
        for cedula in candidatos:
            if cedula == conv.get('cedula'):
                continue  # ya es la cédula validada actual, nada que hacer
            usuario = self.db.buscar_usuario(cedula)
            if usuario:
                self.db.actualizar_conversacion(
                    conv['id'],
                    cedula=cedula,
                    nombre_cliente=usuario['nombre']
                )
                # refrescar la copia local
                conv['cedula'] = cedula
                conv['nombre_cliente'] = usuario['nombre']
                log.info(f"✅ Cédula {cedula} validada → {usuario['nombre']}")
                return conv
        return conv

    def _contexto_cliente(self, conv):
        """
        Construye un mensaje de sistema con los datos REALES del cliente ya validado,
        para que el modelo no los invente ni vuelva a pedir la cédula.
        """
        if not conv or not conv.get('cedula'):
            return None
        usuario = self.db.buscar_usuario(conv['cedula'])
        if not usuario:
            return None

        nombre_corto = self._nombre_corto(usuario.get('nombre'))
        saldo = usuario.get('saldo') or 0

        # Interpretar el estado de la cuenta:
        #   compromiso / exonerado -> activo (servicio funcionando)
        #   cortado(s)             -> servicio cortado (hay que avisar al cliente)
        estado_raw = str(usuario.get('estado_cuenta') or '').strip().lower()
        if 'cort' in estado_raw:
            estado_legible = 'CORTADO'
        elif estado_raw in ('compromiso', 'exonerado', 'activo', ''):
            estado_legible = 'activo'
        else:
            estado_legible = estado_raw

        lineas = [
            "[DATOS VERIFICADOS DEL CLIENTE — usa estos datos reales, NO los inventes]",
            f"- Cédula validada: {conv['cedula']}",
            f"- Trátalo como: Sr. {nombre_corto}",
            f"- Plan actual: {usuario.get('plan') or 'no registrado'}",
            f"- Código de usuario (login portal): {usuario.get('codigo_usuario') or 'no registrado'}",
            f"- Saldo pendiente: ${saldo:,.0f}".replace(',', '.'),
            f"- Estado del servicio: {estado_legible}",
            "La cédula YA fue validada en el sistema. No la vuelvas a pedir.",
        ]

        if estado_legible == 'CORTADO':
            lineas.append(
                "ATENCIÓN: el servicio de este cliente está CORTADO actualmente. "
                "Infórmaselo con tacto y explícale que puede regularizar su situación "
                "con un acuerdo de pago (no se cobra reconexión)."
            )

        return "\n".join(lineas)
    
    def _ejecutar_registrar_caso(self, args, conversacion_id, telefono, mensaje_usuario):
        """
        Ejecuta la herramienta registrar_caso: valida reglas de negocio, genera el
        radicado, crea el caso en la BD y dispara la alerta de Telegram si escala.
        Devuelve un dict que se le entrega al modelo como resultado de la función.
        """
        servicio = (args.get('servicio') or 'otro').strip()
        descripcion = args.get('descripcion') or mensaje_usuario
        datos = args.get('datos') or {}
        requiere_esc = bool(args.get('requiere_escalamiento'))
        tipo_esc = args.get('tipo_escalamiento')

        conv = self.db.obtener_conversacion(telefono)
        cedula = conv.get('cedula') if conv else None
        nombre = conv.get('nombre_cliente') if conv else None

        # Regla de negocio anclada al servidor: no se crean casos sin cédula validada,
        # salvo los tipos que corresponden a clientes nuevos o consultas.
        sin_cedula_ok = servicio in ('afiliacion', 'consulta_cobertura', 'pqr')
        if not cedula and not sin_cedula_ok:
            return {
                "error": "cedula_no_validada",
                "mensaje": ("No se puede crear el caso todavía: primero debes pedir y "
                            "validar la cédula del titular."),
            }

        radicado = self.generar_radicado()
        datos_str = json.dumps(datos, ensure_ascii=False) if isinstance(datos, dict) else str(datos)

        self.db.crear_caso(
            radicado=radicado,
            conversacion_id=conversacion_id,
            tipo=servicio,
            cedula=cedula,
            nombre_cliente=nombre,
            telefono=telefono,
            descripcion=descripcion,
            datos_json=datos_str,
            requiere_escalamiento=requiere_esc,
            tipo_escalamiento=tipo_esc,
        )

        # Notificar SIEMPRE al equipo de sistemas por Telegram (con botones IR TÉCNICO /
        # RESUELTO). Todo caso registrado es accionable: reclamos de internet/TV (orden de
        # revisión), cortes, escalamientos, etc. Si es escalamiento usamos su tipo; si no,
        # usamos el tipo de servicio para el encabezado de la alerta.
        alerta_tipo = tipo_esc if (requiere_esc and tipo_esc) else servicio.upper()
        try:
            from telegram_alerts import enviar_alerta
            enviar_alerta(
                tipo=alerta_tipo,
                radicado=radicado,
                nombre_cliente=nombre or 'Cliente',
                cedula=cedula or 'Pendiente',
                telefono=telefono,
                detalle=descripcion,
            )
        except Exception as e:
            log.warning(f"No se pudo enviar alerta a Telegram: {e}")

        log.info(f"📝 Caso creado vía tool: {radicado} ({servicio}) escalado={requiere_esc}")
        return {"radicado": radicado, "estado": "creado", "escalado": requiere_esc}

    def procesar_mensaje(self, telefono, mensaje, conversacion_id=None):
        if not conversacion_id:
            conv = self.db.obtener_conversacion(telefono)

            # MODO_PRUEBAS: despedida cierra la sesión para que la próxima
            # interacción arranque como un cliente completamente nuevo.
            if self.modo_pruebas and conv and self._es_despedida(mensaje):
                self.db.guardar_mensaje(conv['id'], 'texto', mensaje, 'usuario')
                respuesta_cierre = (
                    "Con gusto, fue un placer atenderle. Si necesita algo más, "
                    "no dude en escribirnos. ¡Que tenga un excelente día! 😊"
                )
                self.db.guardar_mensaje(conv['id'], 'texto', respuesta_cierre, 'sam')
                self.db.actualizar_conversacion(conv['id'], estado='cerrado')
                log.info("🧪 MODO_PRUEBAS: sesión cerrada por despedida. Próximo mensaje = cliente nuevo.")
                return {
                    'exito': True,
                    'respuesta': respuesta_cierre,
                    'respuesta_limpia': respuesta_cierre,
                    'tokens_usados': 0,
                    'conversacion_id': conv['id'],
                    'radicado': None,
                    'escalamiento': None,
                    'tipo_escalamiento': None,
                }

            # MODO_PRUEBAS: un saludo inicia una SESIÓN NUEVA (conversación nueva, sin
            # historial previo). Así no se reconoce al número como cliente recurrente:
            # SAM pide la cédula de cero, como si fuera un cliente nuevo.
            if self.modo_pruebas and conv and self._es_saludo(mensaje):
                self.db.actualizar_conversacion(conv['id'], estado='cerrado')
                conversacion_id = self.db.crear_conversacion(telefono)
                log.info("🧪 MODO_PRUEBAS: nueva sesión (cliente tratado como nuevo).")
            elif conv:
                conversacion_id = conv['id']
            else:
                conversacion_id = self.db.crear_conversacion(telefono)
        
        self.db.guardar_mensaje(
            conversacion_id=conversacion_id,
            tipo='texto',
            contenido=mensaje,
            remitente='usuario'
        )

        # Validación de cédula anclada al servidor (no depende del modelo)
        conv = self.db.obtener_conversacion(telefono)
        conv = self._intentar_validar_cedula(conv, mensaje)

        # Historial: los ÚLTIMOS 20 mensajes en orden cronológico (excluye el actual)
        mensajes_previos = self.db.obtener_mensajes_recientes(conversacion_id, limite=20)
        mensajes_gpt = [
            {"role": "system", "content": self.system_prompt},
            {"role": "system", "content": INSTRUCCION_TOOL},
        ]

        # Inyectar datos reales del cliente validado (evita alucinaciones)
        contexto_cliente = self._contexto_cliente(conv)
        if contexto_cliente:
            mensajes_gpt.append({"role": "system", "content": contexto_cliente})
        else:
            # ¿El cliente intentó dar una cédula que NO existe en el sistema?
            # Se lo decimos explícitamente al modelo para que sea preciso y no adivine.
            posibles = re.findall(r'\d{6,12}', mensaje.replace('.', '').replace(',', ''))
            if posibles and not (conv and conv.get('cedula')):
                nota = (
                    f"[VALIDACIÓN] El documento '{posibles[0]}' NO está registrado en el "
                    "sistema. Indícale al cliente, con el texto exacto: \"El número de "
                    "documento que me indica no se encuentra registrado en nuestro sistema. "
                    "¿Podría verificarlo e intentarlo de nuevo?\" NO generes ningún radicado."
                )
                mensajes_gpt.append({"role": "system", "content": nota})

        for msg in mensajes_previos[:-1]:
            if msg['remitente'] == 'usuario':
                mensajes_gpt.append({"role": "user", "content": msg['contenido']})
            else:
                mensajes_gpt.append({"role": "assistant", "content": msg['contenido']})

        mensajes_gpt.append({"role": "user", "content": mensaje})

        try:
            def _llamada_principal():
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=mensajes_gpt,
                    tools=TOOLS,
                    tool_choice="auto",
                    temperature=0.4,
                    max_tokens=500
                )

            response = _llamar_openai_con_reintentos(_llamada_principal, max_reintentos=3)
            msg_modelo = response.choices[0].message
            tokens_usados = response.usage.total_tokens

            # Datos de caso producidos por la herramienta (fuente de verdad)
            radicado_tool = None
            escalamiento_tool = None
            tipo_escalamiento_tool = None

            if msg_modelo.tool_calls:
                # Reinsertar el turno del asistente con sus tool_calls
                mensajes_gpt.append({
                    "role": "assistant",
                    "content": msg_modelo.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in msg_modelo.tool_calls
                    ],
                })

                for tc in msg_modelo.tool_calls:
                    if tc.function.name == 'registrar_caso':
                        try:
                            args = json.loads(tc.function.arguments)
                        except Exception:
                            args = {}
                        resultado_tool = self._ejecutar_registrar_caso(
                            args, conversacion_id, telefono, mensaje
                        )
                        if resultado_tool.get('radicado'):
                            radicado_tool = resultado_tool['radicado']
                            if args.get('requiere_escalamiento'):
                                escalamiento_tool = True
                                tipo_escalamiento_tool = args.get('tipo_escalamiento')
                    else:
                        resultado_tool = {"error": "funcion_desconocida"}

                    mensajes_gpt.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(resultado_tool, ensure_ascii=False),
                    })

                # Segunda llamada: el modelo redacta la respuesta usando el radicado real
                def _llamada_secundaria():
                    return self.client.chat.completions.create(
                        model=self.model,
                        messages=mensajes_gpt,
                        tools=TOOLS,
                        tool_choice="auto",
                        temperature=0.4,
                        max_tokens=500
                    )

                response2 = _llamar_openai_con_reintentos(_llamada_secundaria, max_reintentos=3)
                msg_modelo = response2.choices[0].message
                tokens_usados += response2.usage.total_tokens

            respuesta = msg_modelo.content or ""

            self.db.guardar_mensaje(
                conversacion_id=conversacion_id,
                tipo='texto',
                contenido=respuesta,
                remitente='sam',
                tokens_usados=tokens_usados
            )

            resultado = self._analizar_respuesta(
                respuesta, conversacion_id, telefono, mensaje,
                radicado_tool=radicado_tool,
                escalamiento_tool=escalamiento_tool,
                tipo_escalamiento_tool=tipo_escalamiento_tool,
            )

            return {
                'exito': True,
                'respuesta': respuesta,
                'tokens_usados': tokens_usados,
                'conversacion_id': conversacion_id,
                **resultado
            }

        except (RateLimitError, APITimeoutError, APIConnectionError) as e:
            log.error(f"Error OpenAI después de reintentos: {type(e).__name__}: {str(e)}")
            respuesta_error = "Perdón, acabo de perder la conexión. ¿Podrías repetir tu pregunta?"
            self.db.guardar_mensaje(
                conversacion_id=conversacion_id,
                tipo='error',
                contenido=respuesta_error,
                remitente='sam'
            )
            return {
                'exito': False,
                'respuesta': respuesta_error,
                'tokens_usados': 0,
                'conversacion_id': conversacion_id,
                'error': f"OpenAI timeout/connection after retries: {str(e)}"
            }
        except APIError as e:
            log.error(f"Error OpenAI fatal: {str(e)}", exc_info=True)
            respuesta_error = "Disculpa, no capté bien lo que me dijiste. ¿Podrías reformular tu pregunta?"
            self.db.guardar_mensaje(
                conversacion_id=conversacion_id,
                tipo='error',
                contenido=respuesta_error,
                remitente='sam'
            )
            return {
                'exito': False,
                'respuesta': respuesta_error,
                'tokens_usados': 0,
                'conversacion_id': conversacion_id,
                'error': f"OpenAI API error: {str(e)}"
            }
        except Exception as e:
            log.error(f"Error inesperado en procesar_mensaje: {str(e)}", exc_info=True)
            respuesta_error = "Déjame ver si entendí bien tu solicitud. ¿Podrías darme más detalles?"
            self.db.guardar_mensaje(
                conversacion_id=conversacion_id,
                tipo='error',
                contenido=respuesta_error,
                remitente='sam'
            )
            return {
                'exito': False,
                'respuesta': respuesta_error,
                'tokens_usados': 0,
                'conversacion_id': conversacion_id,
                'error': str(e)
            }
    
    def _analizar_respuesta(self, respuesta, conversacion_id, telefono, mensaje_usuario,
                            radicado_tool=None, escalamiento_tool=None,
                            tipo_escalamiento_tool=None):
        # Fuente de verdad: lo que produjo la herramienta registrar_caso.
        radicado = radicado_tool
        escalamiento = escalamiento_tool
        tipo_escalamiento = tipo_escalamiento_tool

        # RESPALDO (legacy): solo si el modelo NO usó la herramienta pero igual emitió
        # los marcadores ###DATOS###. Evita crear casos duplicados cuando la tool ya actuó.
        if radicado is None and '###DATOS###' in respuesta and '###FIN###' in respuesta:
            try:
                inicio = respuesta.find('###DATOS###') + len('###DATOS###')
                fin = respuesta.find('###FIN###')
                datos_str = respuesta[inicio:fin].strip()
                datos = json.loads(datos_str)
                tipo_caso = datos.get('servicio', 'otro')
                conv = self.db.obtener_conversacion(telefono)
                cedula = conv.get('cedula') if conv else None
                nombre = conv.get('nombre_cliente') if conv else None
                # Misma regla de negocio: sin cédula validada no se crea (salvo excepciones)
                if cedula or tipo_caso in ('afiliacion', 'consulta_cobertura', 'pqr'):
                    radicado = self.generar_radicado()
                    self.db.crear_caso(
                        radicado=radicado,
                        conversacion_id=conversacion_id,
                        tipo=tipo_caso,
                        cedula=cedula,
                        nombre_cliente=nombre,
                        telefono=telefono,
                        descripcion=mensaje_usuario,
                        datos_json=datos_str
                    )
            except Exception:
                pass

            # Escalamiento por marcador (solo en la ruta de respaldo)
            match_esc = re.search(r'###ESCALAMIENTO###\s*([A-ZÑ_]+)\s*###', respuesta)
            if match_esc:
                escalamiento = True
                tipo_detectado = match_esc.group(1)
                tipos_validos = {'AFILIACION', 'COBERTURA', 'EQUIPO_DAÑADO', 'PQR', 'URGENTE'}
                tipo_escalamiento = tipo_detectado if tipo_detectado in tipos_validos else 'URGENTE'

                if radicado and tipo_escalamiento:
                    try:
                        from telegram_alerts import enviar_alerta
                        conv = self.db.obtener_conversacion(telefono)
                        nombre_cliente = conv.get('nombre_cliente', 'Cliente') if conv else 'Cliente'
                        cedula_cliente = conv.get('cedula', 'Pendiente') if conv else 'Pendiente'
                        enviar_alerta(
                            tipo=tipo_escalamiento,
                            radicado=radicado,
                            nombre_cliente=nombre_cliente,
                            cedula=cedula_cliente,
                            telefono=telefono,
                            detalle=mensaje_usuario
                        )
                    except Exception as e:
                        log.warning(f"No se pudo enviar alerta a Telegram: {e}")

        respuesta_limpia = respuesta
        if '###DATOS###' in respuesta_limpia:
            respuesta_limpia = respuesta_limpia[:respuesta_limpia.find('###DATOS###')].strip()
        if '###ESCALAMIENTO###' in respuesta_limpia:
            respuesta_limpia = respuesta_limpia[:respuesta_limpia.find('###ESCALAMIENTO###')].strip()

        # El modelo a veces inventa el número de radicado en su texto (copia el del
        # ejemplo del prompt). Sustituimos cualquier "VES-XXXX" por el radicado REAL
        # generado por el servidor, para que el cliente vea el mismo que se guardó.
        if radicado:
            respuesta_limpia = re.sub(r'VES-[0-9Xx]{3,}', radicado, respuesta_limpia)

        # Quitar restos de markdown (```), que el modelo a veces deja al envolver
        # el bloque ###DATOS### en un bloque de código.
        respuesta_limpia = respuesta_limpia.replace('```', '')

        # Quitar asteriscos de markdown (** y *): en WhatsApp se verían literales y
        # el cliente debe ver un texto limpio y cálido, no formato de markdown.
        respuesta_limpia = respuesta_limpia.replace('**', '').replace('*', '').strip()

        return {
            'radicado': radicado,
            'escalamiento': escalamiento,
            'tipo_escalamiento': tipo_escalamiento,
            'respuesta_limpia': respuesta_limpia
        }
