import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app) 

def consultar_groq(historial_cliente):
    url_api = "https://api.groq.com/openai/v1/chat/completions"
    clave_api = os.environ.get("GROQ_API_KEY") 
    
    if not clave_api:
        return "Error crítico: La variable GROQ_API_KEY no está configurada en Render."

    mensajes = []
    
    # 1. Inyectamos la personalidad del tutor siempre al principio
    mensajes.append({
        "role": "system",
        "content": "Eres un tutor académico universitario. Eres estricto, claro y directo. Responde siempre en formato de texto plano o Markdown."
    })
    
    # 2. Recortamos a los últimos 6 mensajes para no saturar la cuota gratuita
    historial_recortado = historial_cliente[-6:]
    
    # 3. Limpiamos los datos para asegurar que Groq reciba exactamente lo que pide
    for msg in historial_recortado:
        mensajes.append({
            "role": msg.get("role", "user"),
            "content": str(msg.get("content", ""))
        })
        
    datos = {
        "model": "llama-3.3-70b-versatile", # Modelo activo y actualizado en Groq
        "messages": mensajes,
        "temperature": 0.7
    }
    
    cabeceras = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {clave_api}"
    }
    
    try:
        respuesta = requests.post(url_api, json=datos, headers=cabeceras)
        
        if respuesta.status_code == 200:
            resultado = respuesta.json()
            return resultado["choices"][0]["message"]["content"]
        else:
            # Si falla, ahora devolverá el motivo exacto del rechazo
            return f"Error de la API externa ({respuesta.status_code}): {respuesta.text}"
            
    except Exception as e:
        return f"Error de red interno en el servidor de Python: {str(e)}"

@app.route('/api/chat', methods=['POST'])
def procesar_chat():
    peticion = request.get_json()
    # Ahora recibimos un arreglo de mensajes, no un string suelto
    historial_recibido = peticion.get("historial", [])
    
    if not historial_recibido:
        return jsonify({"respuesta": "Error: El historial llegó vacío al servidor."})
        
    respuesta_ia = consultar_groq(historial_recibido)
    return jsonify({"respuesta": respuesta_ia})

if __name__ == '__main__':
    puerto = int(os.environ.get("PORT", "5000"))
    app.run(host='0.0.0.0', port=puerto)