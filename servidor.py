import os
import re
import html
import time
import logging
import threading
from collections import defaultdict, deque

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import jwt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tutor-ia")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024  # el frontend manda todo el historial; más que esto es abuso

# Solo el frontend propio puede llamar al backend desde un navegador.
# Se pueden sumar orígenes extra (separados por coma) con ALLOWED_ORIGINS en Render.
ORIGENES_PERMITIDOS = ["https://utn-estratega.vercel.app", "http://localhost:5173"]
ORIGENES_PERMITIDOS += [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
CORS(app, origins=ORIGENES_PERMITIDOS)

FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "utn-estratega")

# App Check: prueba que la petición viene de la app real y no de un script.
#   off     -> no se verifica (por defecto)
#   log     -> se verifica y solo se registra en los logs si falla (para medir antes de exigir)
#   enforce -> se rechaza la petición si el token falta o es inválido
APP_CHECK_MODE = os.environ.get("APP_CHECK_MODE", "off")
FIREBASE_PROJECT_NUMBER = os.environ.get("FIREBASE_PROJECT_NUMBER", "")
_jwks_app_check = jwt.PyJWKClient("https://firebaseappcheck.googleapis.com/v1/jwks", lifespan=21600)

# Límites pensados para la capa gratuita de Groq: que un solo usuario no agote la cuota de todos.
LIMITE_POR_MINUTO = 5
LIMITE_POR_DIA = 60

# Buzón de sugerencias: el mail se manda con Resend (https://resend.com) y la clave vive solo en Render.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FEEDBACK_TO = os.environ.get("FEEDBACK_TO", "")  # tu casilla; con la cuenta gratis de Resend tiene que ser la de la cuenta
FEEDBACK_FROM = os.environ.get("FEEDBACK_FROM", "UTN Estratega <onboarding@resend.dev>")
FEEDBACK_ASUNTO = os.environ.get("FEEDBACK_ASUNTO", "Nuevo mensaje en UTN Estratega: {{type}}")
FEEDBACK_POR_MINUTO = 1
FEEDBACK_POR_DIA = 5
FEEDBACK_GLOBAL_POR_DIA = 80   # por debajo de los 100 mails diarios del plan gratis de Resend
FEEDBACK_MAX_CARACTERES = 2000
FEEDBACK_TIPOS = {"Sugerencia", "Error", "Consulta"}
_PLANTILLA_FEEDBACK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plantilla_feedback.html")
# Variables con la sintaxis de EmailJS: {{var}} o {{{var}}}
_VARIABLE = re.compile(r"\{\{\{?\s*(\w+)\s*\}?\}\}")
# Largo máximo por mensaje. Las respuestas del tutor suelen ser largas y vuelven en el historial.
MAX_CARACTERES = {"user": 4000, "assistant": 20000}
ROLES_PERMITIDOS = {"user", "assistant"}  # el cliente nunca puede mandar mensajes "system"

_peticiones_por_uid = defaultdict(deque)
_lock = threading.Lock()
_google_request = google_requests.Request()


def respuesta_error(mensaje, status):
    # El frontend muestra siempre el campo "respuesta", también en los errores.
    return jsonify({"respuesta": mensaje}), status


def verificar_usuario(permitir_invitados=False):
    """Devuelve los datos del token de Firebase si es válido. Por defecto rechaza cuentas de invitado."""
    cabecera = request.headers.get("Authorization", "")
    if not cabecera.startswith("Bearer "):
        return None, respuesta_error("Tenés que iniciar sesión.", 401)

    try:
        datos = id_token.verify_firebase_token(cabecera[7:], _google_request, audience=FIREBASE_PROJECT_ID)
    except Exception as e:
        log.info("Token inválido: %s", e)
        return None, respuesta_error("Tu sesión expiró. Volvé a iniciar sesión.", 401)

    if not datos or datos.get("iss") != f"https://securetoken.google.com/{FIREBASE_PROJECT_ID}":
        return None, respuesta_error("Tu sesión expiró. Volvé a iniciar sesión.", 401)

    if not permitir_invitados and datos.get("firebase", {}).get("sign_in_provider") == "anonymous":
        return None, respuesta_error("El tutor está disponible solo para cuentas registradas.", 403)

    return datos, None


def chequear_app_check():
    """Devuelve una respuesta de error si App Check está en enforce y el token no es válido."""
    if APP_CHECK_MODE in ("log", "enforce"):
        valido, motivo = app_check_valido()
        if not valido:
            log.warning("App Check inválido (%s): %s", APP_CHECK_MODE, motivo)
            if APP_CHECK_MODE == "enforce":
                return respuesta_error("No se pudo verificar la app. Recargá la página y probá de nuevo.", 401)
    return None


def app_check_valido():
    """Verifica el header X-Firebase-AppCheck contra las claves públicas de Firebase."""
    token = request.headers.get("X-Firebase-AppCheck")
    if not token:
        return False, "sin token"
    try:
        clave = _jwks_app_check.get_signing_key_from_jwt(token)
        jwt.decode(token, clave.key, algorithms=["RS256"],
                   audience=f"projects/{FIREBASE_PROJECT_NUMBER}",
                   issuer=f"https://firebaseappcheck.googleapis.com/{FIREBASE_PROJECT_NUMBER}")
        return True, ""
    except Exception as e:
        return False, str(e)


def excede_limite(clave, por_minuto=LIMITE_POR_MINUTO, por_dia=LIMITE_POR_DIA):
    """Ventana deslizante en memoria. Con varios workers de gunicorn, cada uno cuenta por separado."""
    ahora = time.time()
    with _lock:
        tiempos = _peticiones_por_uid[clave]
        while tiempos and ahora - tiempos[0] > 86400:
            tiempos.popleft()
        ultimo_minuto = sum(1 for t in tiempos if ahora - t < 60)
        if ultimo_minuto >= por_minuto or len(tiempos) >= por_dia:
            return True
        tiempos.append(ahora)
        return False


def validar_historial(historial):
    if not isinstance(historial, list) or not historial:
        return None
    limpio = []
    for msg in historial[-6:]:  # solo los últimos 6 mensajes, para no saturar la cuota gratuita
        if not isinstance(msg, dict) or msg.get("role") not in ROLES_PERMITIDOS:
            return None
        contenido = msg.get("content")
        if not isinstance(contenido, str) or len(contenido) > MAX_CARACTERES[msg["role"]]:
            return None
        limpio.append({"role": msg["role"], "content": contenido})
    if limpio[-1]["role"] != "user":
        return None
    return limpio


def consultar_groq(mensajes_usuario):
    clave_api = os.environ.get("GROQ_API_KEY")
    if not clave_api:
        log.error("GROQ_API_KEY no está configurada")
        return None

    mensajes = [{
        "role": "system",
        "content": "Eres un tutor académico universitario. Eres estricto, claro y directo. Responde siempre en formato de texto plano o Markdown."
    }] + mensajes_usuario

    datos = {
        "model": "openai/gpt-oss-120b",  # Modelo ultra estable en la capa gratuita de Groq
        "messages": mensajes,
        "temperature": 0.7
    }
    cabeceras = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {clave_api}"
    }

    try:
        respuesta = requests.post("https://api.groq.com/openai/v1/chat/completions",
                                  json=datos, headers=cabeceras, timeout=60)
        if respuesta.status_code == 200:
            return respuesta.json()["choices"][0]["message"]["content"]
        # El detalle queda en los logs de Render, no se le muestra al usuario.
        log.error("Groq respondió %s: %s", respuesta.status_code, respuesta.text[:500])
    except Exception as e:
        log.exception("Error al llamar a Groq: %s", e)
    return None


@app.route('/api/chat', methods=['POST'])
def procesar_chat():
    error = chequear_app_check()
    if error:
        return error

    usuario, error = verificar_usuario()
    if error:
        return error

    if excede_limite(usuario["sub"]):
        return respuesta_error("Llegaste al límite de mensajes. Esperá un rato y probá de nuevo.", 429)

    peticion = request.get_json(silent=True) or {}
    mensajes = validar_historial(peticion.get("historial"))
    if mensajes is None:
        return respuesta_error("El mensaje es inválido o demasiado largo.", 400)

    respuesta_ia = consultar_groq(mensajes)
    if respuesta_ia is None:
        return respuesta_error("El tutor no está disponible en este momento. Probá en unos minutos.", 502)
    return jsonify({"respuesta": respuesta_ia})


def completar_plantilla(plantilla, variables, es_html=True):
    """Reemplaza las variables de la plantilla. En HTML se escapa siempre lo que escribió el usuario."""
    def valor(m):
        texto = variables.get(m.group(1), "")
        if not es_html:
            return texto.replace("\n", " ")
        return html.escape(texto).replace("\n", "<br>")
    return _VARIABLE.sub(valor, plantilla)


def enviar_mail_feedback(variables, responder_a):
    if not RESEND_API_KEY or not FEEDBACK_TO:
        log.error("Faltan RESEND_API_KEY o FEEDBACK_TO")
        return False
    with open(_PLANTILLA_FEEDBACK, encoding="utf-8") as f:
        cuerpo = completar_plantilla(f.read(), variables)
    datos = {
        "from": FEEDBACK_FROM,
        "to": [FEEDBACK_TO],
        "subject": completar_plantilla(FEEDBACK_ASUNTO, variables, es_html=False)[:200],
        "html": cuerpo,
    }
    if responder_a:
        datos["reply_to"] = responder_a
    try:
        r = requests.post("https://api.resend.com/emails", json=datos, timeout=20,
                          headers={"Authorization": f"Bearer {RESEND_API_KEY}"})
        if r.status_code in (200, 201):
            return True
        log.error("Resend respondió %s: %s", r.status_code, r.text[:500])
    except Exception as e:
        log.exception("Error al llamar a Resend: %s", e)
    return False


@app.route('/api/feedback', methods=['POST'])
def procesar_feedback():
    error = chequear_app_check()
    if error:
        return error

    # Los invitados también pueden dejar sugerencias, como antes con EmailJS
    usuario, error = verificar_usuario(permitir_invitados=True)
    if error:
        return error

    peticion = request.get_json(silent=True) or {}
    tipo = peticion.get("tipo")
    mensaje = peticion.get("mensaje")
    if tipo not in FEEDBACK_TIPOS or not isinstance(mensaje, str) \
            or not mensaje.strip() or len(mensaje) > FEEDBACK_MAX_CARACTERES:
        return respuesta_error("El mensaje es inválido o demasiado largo.", 400)

    if excede_limite("feedback:" + usuario["sub"], FEEDBACK_POR_MINUTO, FEEDBACK_POR_DIA):
        return respuesta_error("Ya enviaste un mensaje hace poco. Esperá un rato y probá de nuevo.", 429)
    if excede_limite("feedback:global", FEEDBACK_GLOBAL_POR_DIA, FEEDBACK_GLOBAL_POR_DIA):
        return respuesta_error("El buzón recibió muchos mensajes hoy. Probá mañana.", 429)

    # El mail del remitente sale del token verificado, nunca de lo que mande el cliente
    email = usuario.get("email")
    variables = {"user_email": email or "Invitado", "type": tipo, "message": mensaje}
    if not enviar_mail_feedback(variables, email):
        return respuesta_error("No se pudo enviar el mail, pero tu mensaje quedó guardado.", 502)
    return jsonify({"respuesta": "ok"})


if __name__ == '__main__':
    puerto = int(os.environ.get("PORT", "5000"))
    app.run(host='0.0.0.0', port=puerto)
