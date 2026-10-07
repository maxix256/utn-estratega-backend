import os
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
# Largo máximo por mensaje. Las respuestas del tutor suelen ser largas y vuelven en el historial.
MAX_CARACTERES = {"user": 4000, "assistant": 20000}
ROLES_PERMITIDOS = {"user", "assistant"}  # el cliente nunca puede mandar mensajes "system"

_peticiones_por_uid = defaultdict(deque)
_lock = threading.Lock()
_google_request = google_requests.Request()


def respuesta_error(mensaje, status):
    # El frontend muestra siempre el campo "respuesta", también en los errores.
    return jsonify({"respuesta": mensaje}), status


def verificar_usuario():
    """Devuelve el uid si el token de Firebase es válido y no es una cuenta de invitado."""
    cabecera = request.headers.get("Authorization", "")
    if not cabecera.startswith("Bearer "):
        return None, respuesta_error("Tenés que iniciar sesión para usar el tutor.", 401)

    try:
        datos = id_token.verify_firebase_token(cabecera[7:], _google_request, audience=FIREBASE_PROJECT_ID)
    except Exception as e:
        log.info("Token inválido: %s", e)
        return None, respuesta_error("Tu sesión expiró. Volvé a iniciar sesión.", 401)

    if not datos or datos.get("iss") != f"https://securetoken.google.com/{FIREBASE_PROJECT_ID}":
        return None, respuesta_error("Tu sesión expiró. Volvé a iniciar sesión.", 401)

    if datos.get("firebase", {}).get("sign_in_provider") == "anonymous":
        return None, respuesta_error("El tutor está disponible solo para cuentas registradas.", 403)

    return datos["sub"], None


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


def excede_limite(uid):
    """Ventana deslizante en memoria. Con varios workers de gunicorn, cada uno cuenta por separado."""
    ahora = time.time()
    with _lock:
        tiempos = _peticiones_por_uid[uid]
        while tiempos and ahora - tiempos[0] > 86400:
            tiempos.popleft()
        ultimo_minuto = sum(1 for t in tiempos if ahora - t < 60)
        if ultimo_minuto >= LIMITE_POR_MINUTO or len(tiempos) >= LIMITE_POR_DIA:
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
    if APP_CHECK_MODE in ("log", "enforce"):
        valido, motivo = app_check_valido()
        if not valido:
            log.warning("App Check inválido (%s): %s", APP_CHECK_MODE, motivo)
            if APP_CHECK_MODE == "enforce":
                return respuesta_error("No se pudo verificar la app. Recargá la página y probá de nuevo.", 401)

    uid, error = verificar_usuario()
    if error:
        return error

    if excede_limite(uid):
        return respuesta_error("Llegaste al límite de mensajes. Esperá un rato y probá de nuevo.", 429)

    peticion = request.get_json(silent=True) or {}
    mensajes = validar_historial(peticion.get("historial"))
    if mensajes is None:
        return respuesta_error("El mensaje es inválido o demasiado largo.", 400)

    respuesta_ia = consultar_groq(mensajes)
    if respuesta_ia is None:
        return respuesta_error("El tutor no está disponible en este momento. Probá en unos minutos.", 502)
    return jsonify({"respuesta": respuesta_ia})


if __name__ == '__main__':
    puerto = int(os.environ.get("PORT", "5000"))
    app.run(host='0.0.0.0', port=puerto)
