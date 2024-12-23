#This is an example that uses the websockets api to know when a prompt execution is done
#Once the prompt execution is done it downloads the images using the /history endpoint

import websocket #NOTE: websocket-client (https://github.com/websocket-client/websocket-client)
import uuid
import json
import urllib.request
import urllib.parse
from flask import Flask, request, jsonify
import base64
import io
import requests
import argparse
import sys

sys.stdout.reconfigure(line_buffering=True)

server_address = "0.0.0.0:8188"


def queue_prompt(prompt, client_id):
    p = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(p).encode('utf-8')
    req =  urllib.request.Request("http://{}/prompt".format(server_address), data=data)
    return json.loads(urllib.request.urlopen(req).read())


def get_image(filename, subfolder, folder_type):
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url_values = urllib.parse.urlencode(data)
    with urllib.request.urlopen("http://{}/view?{}".format(server_address, url_values)) as response:
        return response.read()


def get_history(prompt_id):
    with urllib.request.urlopen("http://{}/history/{}".format(server_address, prompt_id)) as response:
        return json.loads(response.read())


def get_images(ws, prompt, client_id, output_node_id):
    prompt_id = queue_prompt(prompt, client_id)['prompt_id']
    output_images = {}
    while True:
        out = ws.recv()
        print(f"runpod-worker-comfy - websocket message: {out}")
        if isinstance(out, str):
            message = json.loads(out)
            if message['type'] == 'executing':
                data = message['data']
                if data['node'] is None and data['prompt_id'] == prompt_id:
                    break  #Execution is done
        else:
            continue  #previews are binary data

    history = get_history(prompt_id)[prompt_id]
    for node_id in history['outputs']:
        node_output = history['outputs'][node_id]
        print(f"runpod-worker-comfy - node {node_id} output: {node_output}")
        if output_node_id is not None and node_id != output_node_id:
            continue
        images_output = []
        if 'images' in node_output:
            for image in node_output['images']:
                image_data = get_image(image['filename'], image['subfolder'], image['type'])
                images_output.append(image_data)
        if 'gifs' in node_output:
            for image in node_output['gifs']:
                image_data = get_image(image['filename'], image['subfolder'], image['type'])
                images_output.append(image_data)
        output_images[node_id] = images_output

    return output_images


def upload_images(images):
    """
    Upload a list of base64 encoded images to the ComfyUI server using the /upload/image endpoint.

    Args:
        images (list): A list of dictionaries, each containing the 'name' of the image and the 'image' as a base64 encoded string.
        server_address (str): The address of the ComfyUI server.

    Returns:
        list: A list of responses from the server for each image upload.
    """
    if not images:
        return {"status": "success", "message": "No images to upload", "details": []}

    responses = []
    upload_errors = []

    print(f"runpod-worker-comfy - image(s) upload")

    for image in images:
        name = image["name"]
        image_data = image["image"]
        blob = base64.b64decode(image_data)

        # Prepare the form data
        files = {
            "image": (name, io.BytesIO(blob), "image/png"),
            "overwrite": (None, "true"),
        }

        # POST request to upload the image
        response = requests.post(f"http://{server_address}/upload/image", files=files)
        if response.status_code != 200:
            upload_errors.append(f"Error uploading {name}: {response.text}")
        else:
            responses.append(f"Successfully uploaded {name}")

    if upload_errors:
        print(f"runpod-worker-comfy - image(s) upload with errors")
        return {
            "status": "error",
            "message": "Some images failed to upload",
            "details": upload_errors,
        }

    print(f"runpod-worker-comfy - image(s) upload complete")
    return {
        "status": "success",
        "message": "All images uploaded successfully",
        "details": responses,
    }

app = Flask(__name__)


@app.route('/ping')
def ping():
    return "pong"


@app.route('/runsync', methods=['POST'])
def handle_post():
    client_id = str(uuid.uuid4())

    print(f"runpod-worker-comfy - Generation ID: {client_id}")

    data = request.get_json()['input']

    input_images = data['images']

    if input_images is None or data['workflow'] is None:
        return jsonify({'status': 'error', 'message': 'Invalid input: Missing images or workflow'})

    upload_result = upload_images(input_images)

    if upload_result["status"] == "error":
        return upload_result

    ws = websocket.WebSocket()
    ws.connect("ws://{}/ws?clientId={}".format(server_address, client_id))
    images = get_images(ws, data['workflow'], client_id, data['output_node_id'])

    image_base64 = None
    for node_id in images:
        for image_data in images[node_id]:
            image = io.BytesIO(image_data)
            image_bytes = image.getvalue()
            image_base64 = base64.b64encode(image_bytes).decode('utf-8')

    response = {
        'status': 'COMPLETED',
        'output': {
            'message': image_base64
        }
    }
    return jsonify(response)


parser = argparse.ArgumentParser(description="ComfyUI Middleware")

parser.add_argument("--port", type=int, default=3000, help="Port to run the Flask server on")

args = parser.parse_args()

if __name__ == '__main__':
    # Run the Flask development server
    app.run(debug=True, port=args.port, host="0.0.0.0")
