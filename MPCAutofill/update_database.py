#!/usr/bin/env python3.7
import sqlite3
import os.path
from googleapiclient.errors import HttpError
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
import time
import imageio
import os
import datetime
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from tqdm import tqdm
from to_searchable import to_searchable
from credentials import drive_client_id, drive_client_secret, drive_refresh_token


MY_NAME = 'Alex'
MY_FULL_NAME = 'Alessandro Longoni'
DRIVE_TOKEN_URI = 'https://oauth2.googleapis.com/token'
MY_PROXY_DIR_NAME = 'Commander Staples'
MY_PROXY_DIR_ID = '<INSERT_ID_HERE>'
MY_BACK_DIR_NAME = 'Card Backs'

# If modifying these scopes, delete the file token.pickle.
SCOPES = ['https://www.googleapis.com/auth/drive']

global SOURCES
SOURCES = {
    MY_NAME: {
        "quantity": 0,
        "username": MY_NAME,
        "reddit": "",
        "drivelink": "https://drive.google.com/open?id=" + MY_PROXY_DIR_ID,
        "description": "Virus cards"
    },
    MY_NAME + "_cardbacks": {
        "quantity": 0,
        "username": MY_NAME,
        "reddit": "",
        "drivelink": "https://drive.google.com/open?id=" + MY_PROXY_DIR_ID,
        "description": "custom cardbacks rendered at 1200 DPI in Photoshop"
    },
    "Unknown": {"quantity": 0,
                       "username": "",
                       "reddit": "",
                       "drivelink": "",
                       "description": ""}
           }

DPI_HEIGHT_RATIO = 300/1100  # 300 DPI for image of vertical resolution 1100 pixels


def create_connection(db_file):
    conn = None
    try:
        conn = sqlite3.connect(db_file)
    except sqlite3.Error as e:
        print(e)
    return conn


def fill_tables(conn):
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS cardpicker_card (
        id text PRIMARY KEY,
        name text,
        priority integer,
        source text,
        dpi integer,
        searchq text,
        thumbpath text);""")

    db_datetime = datetime.datetime.fromtimestamp(os.path.getmtime("./card_db.db")) - datetime.timedelta(days=1)
    print("Date & time to check for updates to thumbnails for: " + str(db_datetime))

    # add every card in my google drive to the database,
    # downloading its thumbnail and putting it in the thumbnail folder.

    # Call to google drive API
    service = login()
    results = service.files().list(
        q="mimeType='application/vnd.google-apps.folder'",
        fields="files(id, name, parents, driveId)",
        pageSize=1000
    ).execute()

    folders = results.get('files', [])
    print("Folders found: {}".format(len(folders)))

    # Skip some folders as specified
    folders = [f for f in folders if f['name'] == MY_PROXY_DIR_NAME]
    print("Folders found: {}".format(len(folders)))

    queries = []
    for folder in folders:
        queries.append(search_folder(folder, db_datetime))
    # search_folder(c, folders[1], db_datetime)

    # Commit changes all at once
    # Clear table to ensure only available cards are included
    c.execute("DELETE FROM cardpicker_card;")
    for queryset in queries:
        for card in queryset:
            if not card:
                continue
            c.execute("""INSERT OR REPLACE INTO cardpicker_card VALUES (?,?,?,?,?,?,?)""", card)
    conn.commit()


def search_folder(folder, db_datetime):
    print("Searching drive: {}".format(folder['name']))

    folderList = [folder]
    imageList = []
    folderDict = {}
    parentDict = {}

    while len(folderList) > 0:
        # Add all folders within the current folder to the folder list
        # Add all images within the current folder to the images list
        # Remove the current folder from the folder list
        # The next current folder is the 1st element in the folder list
        time.sleep(0.1)
        currFolder = folderList[0]
        print("Searching: {}".format(currFolder['name']))
        folderDict[currFolder['id']] = currFolder['name']
        try:
            parentDict[currFolder['id']] = folderDict[currFolder['parents'][0]]
        except KeyError:
            parentDict[currFolder['id']] = ""

        # Search for folders within the current folder
        while True:
            try:
                results = service.files().list(
                    q="mimeType='application/vnd.google-apps.folder' and "
                      "'{}' in parents".format(currFolder['id']),
                    fields="files(id, name, parents)",
                    pageSize=500
                ).execute()
                folderList += results.get('files', [])
                break
            except HttpError:
                pass

        # Search for images with paging - probably not necessary for folders
        page_token = None
        while True:
            # Search for all images within this folder
            time.sleep(0.1)
            try:
                results = service.files().list(
                    q="(mimeType contains 'image/png' or "
                      "mimeType contains 'image/jpg' or "
                      "mimeType contains 'image/jpeg') and "
                      "'{}' in parents".format(currFolder['id']),
                    fields="nextPageToken, "
                           "files(id, name, trashed, properties, parents, modifiedTime, imageMediaMetadata, owners)",
                    pageSize=500,
                    pageToken=page_token
                ).execute()
            except HttpError:
                # TODO: Not pass?
                pass

            if len(results['files']) <= 0:
                break

            print("Found {} image(s)".format(len(results.get('files', []))))

            # Append the retrieved images to the image list
            imageList += results.get('files', [])

            page_token = results.get('nextPageToken', None)
            if page_token is None:
                break
        folderList.remove(currFolder)

    print("Number of images found: {}".format(len(imageList)))
    print("Folder dict:")
    for key in folderDict:
        print("{}: {}".format(key, folderDict[key]))

    queries = []

    # add the retrieved cards to the database, parallelised by 20 for speed
    # TODO: Is 20 the right number here?
    with tqdm(total=len(imageList), desc="Inserting into DB") as bar, ThreadPoolExecutor(max_workers=20) as pool:
        for result in pool.map(partial(add_card, folderDict, parentDict, folder, db_datetime), imageList):
            queries.append(result)
            # c.execute("""INSERT OR REPLACE INTO cardpicker_card VALUES (?,?,?,?,?,?,?)""", result)
            bar.update(1)

    print("Finished crawling {}".format(folder['name']))
    print("")
    return queries


def add_card(folderDict, parentDict, folder, db_datetime, item):
    if not item['trashed']:
        folderName = folderDict[item['parents'][0]]
        parentName = parentDict[item['parents'][0]]

        owner = item['owners'][0]['displayName']

        folders_sources = {
            MY_FULL_NAME: MY_NAME
        }

        scryfall = False
        priority = 2
        if "Retro Cube" in parentName:
            priority = 0
        if ")" in item['name']:
            priority = 1
        source = "Unknown"
        if folder['name'] == MY_PROXY_DIR_NAME:
            source = MY_NAME
            if folderName == MY_BACK_DIR_NAME:
                if "Black Lotus" in item['name']:
                    priority += 10
                source += "_cardbacks"
                priority += 5

        elif owner in folders_sources.keys():
            source = folders_sources[owner]

        elif folder['name'] == "MPC Scryfall Scans":
            source = "berndt_toast83/" + folderName
            scryfall = True

        if "Basic" in folderName:
            priority += 5

        if scryfall:
            SOURCES["berndt_toast83"]["quantity"] += 1
        else:
            SOURCES[source]["quantity"] += 1
        # folder_path = "./../staticroot/cardpicker/" + source
        folder_path = "cardpicker/static/cardpicker/" + source

        folder_path = os.path.abspath(folder_path)

        # Download card thumbnail if necessary
        file_datetime = datetime.datetime.strptime(
            item["modifiedTime"], "%Y-%m-%dT%H:%M:%S.%fZ"
        )

        thumbnail_path = folder_path + "/" + item['id'] + ".png"
        try:
            if not os.path.exists(folder_path):
                os.makedirs(folder_path)
        except FileExistsError:
            pass

        # Calculate source image DPI, rounded to tens
        dpi = 10 * round(int(item['imageMediaMetadata']['height']) * DPI_HEIGHT_RATIO / 10)

        if not os.path.isfile(thumbnail_path) or file_datetime > db_datetime:
            # three tries at downloading the file
            counter = 0
            while counter < 3:
                try:
                    # Read thumbnail
                    thumbnail = imageio.imread(
                        "https://drive.google.com/thumbnail?sz=w400-h400&id=" + item['id']
                    )

                    # Trim off 13 pixels around the edges, which should remove the print bleed edge,
                    # assuming the image is 293 x 400 in resolution, before writing to disk
                    imageio.imwrite(thumbnail_path, thumbnail[13:-13, 13:-13, :])
                    break
                except:  # TODO: Not bare except
                    counter += 1
            if counter >= 3:
                print("Failed to download thumbnail for: {}".format(item['name']))

        # Remove the file extension from card name
        cardname = '.'.join(item['name'].split(".")[0:-1])

        # Store the image's static URL as well
        static_url = "cardpicker/" + source + "/" + item['id'] + ".png"

        # Return card info so we can insert into database
        return item['id'], cardname, priority, source, dpi, to_searchable(cardname), static_url


def add_sources(conn):
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS cardpicker_source (
    id text PRIMARY KEY,
    quantity integer,
    username text,
    reddit text,
    drivelink text,
    description text);""")

    source_ids = list(SOURCES.keys())
    source_ids.remove("Unknown")
    # source_ids.remove("Chilli_Axe_cardbacks")
    for source_id in source_ids:
        c.execute("INSERT OR REPLACE INTO cardpicker_source VALUES (?,?,?,?,?,?)",
                  (source_id,
                   SOURCES[source_id]['quantity'],
                   SOURCES[source_id]['username'],
                   SOURCES[source_id]['reddit'],
                   SOURCES[source_id]['drivelink'],
                   SOURCES[source_id]['description']))
    conn.commit()


def login():
    credentials = Credentials(
        token=None,
        refresh_token=drive_refresh_token,
        token_uri=DRIVE_TOKEN_URI,
        client_id=drive_client_id,
        client_secret=drive_client_secret,
        scopes=SCOPES
    )

    return build('drive', 'v3', credentials=credentials)


if __name__ == "__main__":
    with create_connection("./card_db.db") as conn:
        service = login()
        t = time.time()
        fill_tables(conn)
        add_sources(conn)
        print(SOURCES)
        print("Elapsed time: {} minutes.".format((time.time() - t) / 60))
    print("Finished.")
