import pyrosm
from geopy.geocoders import Nominatim
import re
import matplotlib.pyplot as plt
from ollama import chat, ChatResponse
import math
from googlesearch import search
import requests
from argparse import ArgumentParser
import logging
import subprocess
import time
import tqdm
from shapely import Point


def oracle(_content, _model):
    # Our chatbot that happily translates for us
    response: ChatResponse = chat(model='qwen2.5:' + _model, messages=[
        {
            'role': 'user',
            'content': _content
        },
    ])
    return(response.message.content)


def get_user_address(input_address):
    geolocator = Nominatim(user_agent='tag_finder')
    user_location = geolocator.geocode(input_address, addressdetails=True)

    if user_location is None:
        raise Exception("Inputted address could not be found")

    standardize_letters = lambda x: x.replace("ü", "ue").replace("ä", "ae").replace("ö", "oe").replace("ß", "ss")
    user_address = {
                        'housenumber':                          user_location.raw['address']['house_number'],
                        'street':           standardize_letters(user_location.raw['address']['road']),
                        'state':            standardize_letters(user_location.raw['address']['state']),
    }
    try:
        user_address['city'] = standardize_letters(user_location.raw['address']['city'])    
    except:
        try:
            user_address['city'] = standardize_letters(user_location.raw['address']['village'])
        except:
            user_address['city'] = ""
    return (user_address, user_location)


# Latitude: 1 deg = 110.574 km
# Longitude: 1 deg = 111.320*cos(latitude) km

def get_bounding_box(radius, user_location):
    # Box for the Geosearch - is of size radius*2 x radius*2
    latitude_radius = float(radius) / 110.574
    longitude_radius = float(radius) / (111.320 * math.cos(math.radians(user_location.latitude)))
    return [user_location.longitude - longitude_radius, user_location.latitude - latitude_radius, user_location.longitude + longitude_radius, user_location.latitude + latitude_radius]


def get_distance(curr_lon: float, curr_lat: float, target_lon: float, target_lat: float) -> float:
    # Haversine formula for distance on a globe
    
    curr_lat, curr_lon, target_lat, target_lon = map(math.radians, [curr_lat, curr_lon, target_lat, target_lon])

    d_lat = target_lat - curr_lat
    d_lon = target_lon - curr_lon
    a = math.sin(d_lat / 2)**2 + math.cos(curr_lat) * math.cos(target_lat) * math.sin(d_lon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    return 6371.0 * c


def get_data(user_address):
    # We can't use the entirety of germany as that would take too much ram.
    # First we try and see if there if data for our city - If we live in the countryside we need to use the entire state.
    logging.info('Starting download of PBF data...')
    try:
        fp = pyrosm.get_data(user_address["city"])
    except: 
        try:
            logging.info(f"Could not find data for '{user_address['city']}', using '{user_address['city'][:user_address['city'].find(' ')]}' instead.")
            fp = pyrosm.get_data(user_address["city"][:user_address["city"].find(' ')]) # Turn cities like 'Freiburg im Breisgau' into 'Freiburg'.
        except:
            logging.info(f"Could not find PBF data for '{user_address['city']}'. Loading entire area '{user_address['state']}' instead...")

            try:
                fp = pyrosm.get_data(user_address["state"])
            except:
                try:
                    fp = pyrosm.get_data(user_address["state"][:user_address["state"].find(' ')])
                except:
                    logging.CRITICAL(f"Couldn't find PBF data for {user_address['state']}. Aborting.")
                    quit()
    logging.info('Completed download of PBF data.')
    return fp


def get_rows(target_pois, user_location, radius):
    # Extracts specifically distances, names, and phones from the dataframe
    targets = {}
 
    # Extract data into dict, sanitize, then sort it
    for index, row in target_pois.iterrows():
        name = row['name']
        phone = row['phone']

        if name is None:
            name = "Name not specified"
        if phone is None:
            phone = "Phone not specified"

        targets.update({(name, phone) : get_distance(user_location.longitude, user_location.latitude, row['lon'], row['lat'])})

    targets = {k: v for k, v in sorted(targets.items(), key=lambda item: item[1])}

    phones = [x[0][1] for x in targets.items()]
    names = [x[0][0] for x in targets.items()]
    distances = [x[1] for x in targets.items()]

    for i in range(len(distances)):
        if i >= len(distances):
            break
        if distances[i] > float(radius):
            phones.pop(i)
            names.pop(i)
            distances.pop(i)

    if len(distances) == 0:
        logging.critical("No target found in radius. Either the OSM tag is not valid, or no objects are in the radius.")
        quit()

    return (distances, names, phones)


def fix_missing_coordinates(df):

    try:
        a = target_pois.iloc[0]['lat']
        a = target_pois.iloc[0]['lon']
    except:
        target_pois.insert(-1, 'lat', math.nan)
        target_pois.insert(-1, 'lon', math.nan)
        
    for index, row in df.iterrows():
        if math.isnan(row['lat']) or math.isnan(row['lon']):
            if row['geometry'] is not None:
                try: # geometry: shapely.polygon.Polygon
                    bbox = row['geometry'].exterior.bounds
                except: # geometry: shapely.MultiLineString
                    bbox = row['geometry'].bounds
                df.at[index, 'lon'] = bbox[0]
                df.at[index, 'lat'] = bbox[1]
                df.at[index, 'geometry'] = Point(bbox[0], bbox[1])

def get_target_urls(search_terms):
    logging.info('Fetching target urls...')
    # Use an automated google search to get urls of the targets' websites
    target_urls = []
    for search_term in tqdm.tqdm(search_terms):
        #logging.info(search_term)
        if search_term == "":
            target_urls.append("")
            continue
        urls = search(search_term, num_results=2, lang="de")
        url = next(urls)
        if 'search?' in url:
            url = ""
        target_urls.append(url)
    logging.info('Fetched target urls.')
    return target_urls


def fetch_html(_url):
    # Simple webscraper
    if _url == "":
        return ""
    return requests.get(_url).text
    
def sanitize_phone(_unsanitized_phone):
    return re.sub(r'\D', '', _unsanitized_phone)


def get_target_scraped_phones(target_urls):
    # Get the phone numbers from the scraped website data
    target_scraped_phones = []
    logging.info("Starting web scraping...")

    for url in target_urls:
        if url == "":
            target_scraped_phones.append("Phone not specified")
            continue
        html = fetch_html(url)
        try:
            # This was a real pain...
            unsanitized_phone = re.search(r'(((\+49)( (\(0\)) )?|0\d{3,4}[\/ ])[^#%:;{},.\d\n]{0,3}(\d{3,10})[^#%:;{},.\d\n\w]?(\d{3,7})[^#%:;{},.\d\n\w]?(\d{2,4})?[^#%:;{},.\d\n\w]?(\d{0,2})?)', html).group(0)
        except:
            unsanitized_phone = ""
        phone = sanitize_phone(unsanitized_phone)
        target_scraped_phones.append(phone)

    logging.info('Completed web scraping.')
    return target_scraped_phones


def get_combined_phones(phones, target_scraped_phones):
    # Combine the two datasets of phone numbers
    combined_phones = phones.copy()

    for i, x in enumerate(phones):
        if target_scraped_phones[i] == "":
            pass
        elif sanitize_phone(x) == target_scraped_phones[i]:
            pass
        else:
            combined_phones[i] = target_scraped_phones[i]

    return combined_phones


if __name__ == '__main__':
    parser = ArgumentParser(prog='main.py', description="Finds OSM objects, and lists their distance and phone number. Here's the of tags: https://wiki.openstreetmap.org/wiki/Map_features.", epilog='The main performace impact is the map used. Due to the data structure OSM uses, using a smaller radius means getting all objects and then filtering which ones are inside the radius, meaning the radius has little impact on the performance.')
    parser.add_argument('input_tag', help="OSM tag or OSM key (has to be exact), e.g. 'healthcare=psychotherapist', 'healthcare', 'amenity=biergarten'.")
    parser.add_argument('input_address', help="The address you want to find objects close to (doesn't have to be exact), e.g. 'Idinger Str 1 Freiburg'.")
    parser.add_argument('-r', '--radius', help='The radius in km around you to search. If not specified, is 3 km. Bounded by the size of your city.')
    parser.add_argument('-m', '--map', help="Specifically load a certain map. Has to be on Geofabrik or BBBike, e.g. 'Freiburg', 'Baden-Württemburg'.") 
    parser.add_argument('-ns', '--noscrape', action='store_true', help="Don't scrape for phone munbers. Makes the program faster. Will auto-turn-on if more than 50 targets are found (so that google doesn't block you).")

    args = parser.parse_args()

    # Configure the logging object
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s][%(levelname)s] %(message)s')
    logging.basicConfig(level=logging.WARNING, format='[%(asctime)s][%(levelname)s] %(message)s')
    logging.basicConfig(level=logging.ERROR, format='[%(asctime)s][%(levelname)s] %(message)s')
    logging.basicConfig(level=logging.CRITICAL, format='[%(asctime)s][%(levelname)s] %(message)s')


    input_address = args.input_address
    input_tag = args.input_tag # this needs to be a valid OSM tag
    radius = 3 if args.radius is None else args.radius # in km

    if int(radius) < 0:
        raise Exception("Negative radius is not allowed.")

    # User address: dictionary with address, User location: raw data
    user_address, user_location = get_user_address(input_address)
    # Handling of the -c argument
    user_address['city'] = user_address['city'] if args.map is None else args.map
    # Box for the Geosearch - is of size radius*2 x radius*2
    bounding_box = get_bounding_box(radius, user_location)

    # Download the OSM data as a PBF file
    fp = get_data(user_address)
    # Initialize the OSM object of correct size
    osm = pyrosm.OSM(fp, bounding_box=bounding_box)

    # Extract the input tag
    if "=" in input_tag:
        (OSM_key, OSM_value) = input_tag.split("=")
    else:
        (OSM_key, OSM_value) = input_tag, None

    # Get all points of interest (POIs) in the OSM object
    custom_filter = {OSM_key : True}

    logging.info(f'Filling dataframe...')
    try:
        pois = osm.get_pois(custom_filter=custom_filter, extra_attributes=[OSM_key])
    except:
        raise Exception(f"Not enough ram to load the map {fp}.")
    
    try:
        pois["poi_type"] = pois[OSM_key]
    except:
        raise Exception(f"{OSM_key} is not a valid OSM key.")

    # Sort the specific ones we're looking for
    if OSM_value == None:
        target_pois = pois
    else:
        target_pois = pois.copy()[pois["poi_type"] == OSM_value]

    fix_missing_coordinates(target_pois)
    

    # Extract useable data from the dataframe
    distances, names, phones = get_rows(target_pois, user_location, radius)
    logging.info(f'Dataframe filled. {len(distances)} Entries.')

    if len(distances) > 50:
        args.noscrape = True
        logging.info(f'Due to size of Dataframe: {target_pois.shape[0]} scraping was turned off (Google would block you for suspicious traffic otherwise).')

    if args.noscrape is False:
        # Translate the tag for our search engine
        translated_OSM_value = oracle(OSM_key if OSM_value is None else OSM_value, 'translate')

        # Initialize our search terms
        search_terms = [name + " " + translated_OSM_value + " " + user_address["city"] if name != "Name not specified" else "" for name in names]
        # Get our found URLs
        target_urls = get_target_urls(search_terms)
        # Scrape the phone numbers
        target_scraped_phones = get_target_scraped_phones(target_urls)
        # Combine both datasets of phone numbers
        combined_phones = get_combined_phones(phones, target_scraped_phones)
    else:
        combined_phones = phones.copy()


    # Format the final output
    subprocess.run(["echo", " "])
    subprocess.run(["echo", " distance" + "    Name".ljust(44) + "Phone"])
    subprocess.run(["echo", " "])
    truncate = 100 < len(distances)
    for i in range(len(distances)):
        subprocess.run(["echo", str(distances[i])[:4] + " km" + "    " + (str(names[i])).ljust(40) + str(combined_phones[i])])
        if (truncate and i >= 100):
            subprocess.run(["echo", " ..."])