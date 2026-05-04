import csv
import os
import ftplib
import io
import time
import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, '.env'))

FTP_HOST  = os.environ.get('FTP_HOST')
FTP_USER  = os.environ.get('FTP_USER')
FTP_PASS  = os.environ.get('FTP_PASS')
SRC_FILE  = 'ATD_Wheels_Inventory.csv'

SHOPIFY_STORE_URL    = os.environ.get('SHOPIFY_STORE_URL')
SHOPIFY_ACCESS_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN')
SHOPIFY_LOCATION_ID  = os.environ.get('SHOPIFY_LOCATION_ID', 'gid://shopify/Location/91638464747')  # ATD Warehouse


def download_csv():
    print(f"Downloading {SRC_FILE} from {FTP_HOST}...", flush=True)
    buf = io.BytesIO()
    ftp = ftplib.FTP(FTP_HOST, timeout=60)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.retrbinary(f'RETR {SRC_FILE}', buf.write)
    ftp.quit()
    buf.seek(0)
    print("Download complete.", flush=True)
    return buf


def parse_inventory(buf):
    inventory = {}
    reader = csv.DictReader(io.StringIO(buf.read().decode('utf-8', errors='replace')))
    for row in reader:
        pn = row.get('Pn', '').strip()
        qty = row.get('Inventory', '0').strip()
        if pn:
            inventory[pn] = int(qty) if qty else 0
    print(f"Loaded {len(inventory):,} wheel SKUs.", flush=True)
    return inventory


def shopify_graphql_request(query, variables=None):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/graphql.json"
    headers = {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}
    while True:
        try:
            r = requests.post(url, headers=headers, json={'query': query, 'variables': variables})
            data = r.json()
            if 'errors' in data:
                for error in data['errors']:
                    if error.get('extensions', {}).get('code') == 'THROTTLED':
                        time.sleep(5)
                        break
                else:
                    return data
            else:
                cost = data.get('extensions', {}).get('cost', {})
                if cost.get('throttleStatus', {}).get('currentlyAvailable', 1000) < 500:
                    time.sleep(2)
                return data
        except Exception as e:
            print(f"Shopify request error: {e}", flush=True)
            time.sleep(2)


def sync_shopify(inventory):
    print("Fetching Shopify variants...", flush=True)
    shopify_data = {}
    has_next_page = True
    cursor = None
    while has_next_page:
        query = """
        query getVariants($cursor: String) {
          productVariants(first: 250, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            edges { node { id sku inventoryItem { id } } }
          }
        }
        """
        res = shopify_graphql_request(query, {'cursor': cursor})
        if not res or 'data' not in res:
            break
        for edge in res['data']['productVariants']['edges']:
            node = edge['node']
            if node['sku']:
                shopify_data[node['sku']] = node
        page_info = res['data']['productVariants']['pageInfo']
        has_next_page = page_info.get('hasNextPage', False)
        cursor = page_info.get('endCursor')

    print(f"Shopify variants loaded: {len(shopify_data):,}", flush=True)

    items_to_set = []
    for sku, node in shopify_data.items():
        if sku in inventory:
            items_to_set.append({
                'inventoryItemId': node['inventoryItem']['id'],
                'locationId': SHOPIFY_LOCATION_ID,
                'quantity': inventory[sku]
            })

    if not items_to_set:
        print("No matching SKUs found.", flush=True)
        return

    print(f"Syncing {len(items_to_set):,} wheels to Shopify ATD Warehouse...", flush=True)
    for i in range(0, len(items_to_set), 100):
        batch = items_to_set[i:i+100]
        shopify_graphql_request("""
            mutation inventorySetOnHandQuantities($input: InventorySetOnHandQuantitiesInput!) {
              inventorySetOnHandQuantities(input: $input) { userErrors { message } }
            }
        """, {"input": {"reason": "correction", "setQuantities": batch}})
    print("Shopify inventory sync complete.", flush=True)


def main():
    if not all([FTP_HOST, FTP_USER, FTP_PASS, SHOPIFY_STORE_URL, SHOPIFY_ACCESS_TOKEN]):
        print("ERROR: Missing credentials in .env")
        return

    print("=== ATD Wheels Inventory Sync ===", flush=True)
    buf = download_csv()
    inventory = parse_inventory(buf)

    if not inventory:
        print("No inventory data found. Aborting.")
        return

    sync_shopify(inventory)
    print("All done.", flush=True)


if __name__ == "__main__":
    main()