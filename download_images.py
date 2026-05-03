import sqlite3
import requests
import os

API_KEY = "PASTE_YOUR_PEXELS_KEY_HERE"

headers = {
    "Authorization": API_KEY
}

DB = "nutrilens.db"
SAVE_DIR = "static/recipe_images"

os.makedirs(SAVE_DIR, exist_ok=True)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
recipes = conn.execute("SELECT id, name FROM recipes").fetchall()
conn.close()

for recipe in recipes:

    recipe_id = recipe["id"]
    name = recipe["name"]

    print("Downloading:", name)

    url = f"https://api.pexels.com/v1/search?query={name}+food&per_page=1"

    try:
        res = requests.get(url, headers=headers)
        data = res.json()

        if data["photos"]:
            image_url = data["photos"][0]["src"]["large"]

            img = requests.get(image_url)

            with open(f"{SAVE_DIR}/{recipe_id}.jpg", "wb") as f:
                f.write(img.content)

    except:
        print("Failed:", name)

print("Finished downloading images")