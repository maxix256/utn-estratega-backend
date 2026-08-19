import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app) 

def consultar_groq(mensaje_del_alumno):
    url_api = "https://api.groq.com/openai/v1/chat/completions"
    
    # Leemos la clave desde las variables de entorno de la nube
    clave_api = os.environ.get("GROQ_API_KEY") 
    
    if clave_api == None:
        return "Error crítico: Falta configurar la API Key en el servidor."

    # Armamos el arreglo de mensajes
    mensajes = []
    
    mensaje_sistema = {}
    mensaje_sistema["role"] = "system"
    mensaje_sistema["content"] = "Eres un tutor académico de la UTN FRVM. Eres estricto, claro y directo. Ayudas a los estudiantes de Ingeniería en Sistemas."
    mensajes.append(mensaje_sistema)
    
    mensaje_cliente = {}
    mensaje_cliente["role"] = "user"
    mensaje_cliente["content"] = mensaje_del_alumno
    mensajes.append(mensaje_cliente)
    
    # Armamos los datos del modelo
    datos = {}
    # Usamos el modelo Gemma de 9 billones de parámetros alojado en Groq
    datos["model"] = "gemma2-9b-it" 
    datos["messages"] = mensajes
    datos["temperature"] = 0.7
    
    # Cabeceras de seguridad HTTP
    cabeceras = {}
    cabeceras["Content-Type"] = "application/json"
    cabeceras["Authorization"] = "Bearer " + clave_api
    
    try:
        respuesta = requests.post(url_api, json=datos, headers=cabeceras)
        
        if respuesta.status_code == 200:
            resultado = respuesta.json()
            texto_final = resultado["choices"][0]["message"]["content"]
            return texto_final
        else:
            return "Error de la API externa: " + str(respuesta.status_code)
            
    except Exception:
        return "Error de red interno del servidor de Python."

@app.route('/api/chat', methods=['POST'])
def procesar_chat():
    peticion = request.get_json()
    mensaje_recibido = peticion["mensaje"]
    
    respuesta_ia = consultar_groq(mensaje_recibido)
    
    respuesta_final = {}
    respuesta_final["respuesta"] = respuesta_ia
    
    return jsonify(respuesta_final)

if __name__ == '__main__':
    # En la nube, el puerto no es siempre 5000. Lo lee del sistema.
    puerto_str = os.environ.get("PORT", "5000")
    puerto = int(puerto_str)
    
    # Host 0.0.0.0 significa que permite conexiones desde internet
    app.run(host='0.0.0.0', port=puerto)