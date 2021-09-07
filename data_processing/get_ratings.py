#!/usr/local/bin/python3.9

import requests
import time
from tqdm import tqdm
import math
from itertools import chain

import asyncio
from aiohttp import ClientSession
from bs4 import BeautifulSoup

from pprint import pprint

import pymongo
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError

if __name__ == "__main__":
    from utils import utils
else:
    from data_processing.utils import utils


async def fetch(url, session, input_data={}):
    async with session.get(url) as response:
        return await response.read(), input_data
            

async def get_page_counts(usernames, users_cursor):
    url = "https://letterboxd.com/{}/films/"
    tasks = []

    async with ClientSession() as session:
        for username in usernames:
            task = asyncio.ensure_future(fetch(url.format(username), session, {"username": username}))
            tasks.append(task)
        
        responses = await asyncio.gather(*tasks)

        update_operations = []
        for i, response in enumerate(responses):
            soup = BeautifulSoup(response[0], "lxml")
            try:
                page_link = soup.findAll("li", attrs={"class", "paginate-page"})[-1]
                num_pages = int(page_link.find("a").text.replace(',', ''))
            except IndexError:
                num_pages = 1

            # users_cursor.update_one({"username": response[1]['username']}, {"$set": {"num_ratings_pages": num_pages}})   
            update_operations.append(
                UpdateOne({
                    "username": response[1]['username']
                    },
                    {"$set": {"num_ratings_pages": num_pages}},
                    upsert=True
                )
            )

        try:
            if len(update_operations) > 0:
                users_cursor.bulk_write(update_operations, ordered=False)
        except BulkWriteError as bwe:
            pprint(bwe.details)


async def generate_ratings_operations(response, send_to_db=True, return_unrated=False):
    
    # Parse ratings page response for each rating/review, use lxml parser for speed
    soup = BeautifulSoup(response[0], "lxml")
    reviews = soup.findAll("li", attrs={"class": "poster-container"})

    # Create empty array to store list of bulk operations or rating objects
    ratings_operations = []
    movie_operations = []

    # For each review, parse data from scraped page and append an UpdateOne operation for bulk execution or a rating object
    for review in reviews:
        movie_id = review.find('div', attrs={"class", "film-poster"})['data-target-link'].split('/')[-2]

        rating = review.find("span", attrs={"class": "rating"})
        if not rating:
            if return_unrated == False:
                continue
            else:
                rating_val = -1
        else:
            rating_class = rating['class'][-1]
            rating_val = int(rating_class.split('-')[-1])

        rating_object = {
                    "movie_id": movie_id,
                    "rating_val": rating_val,
                    "user_id": response[1]["username"]
                }
        
        # We're going to eventually send a bunch of upsert operations for movies with just IDs
        # For movies already in the database, this won't impact anything
        # But this will allow us to easily figure out which movies we need to scraped data on later,
        # Rather than scraping data for hundreds of thousands of movies everytime there's a broader data update
        skeleton_movie_object = {
            "movie_id": movie_id
        }

        # If returning objects, just append the object to return list
        if not send_to_db:
            ratings_operations.append(rating_object)

        # Otherwise return an UpdateOne operation to bulk execute
        else:
            ratings_operations.append(UpdateOne({
                    "user_id": response[1]["username"],
                    "movie_id": movie_id
                },
                {
                    "$set": rating_object
                },
                    upsert=True
                )
            )

            movie_operations.append(UpdateOne({
                    "movie_id": movie_id
                },
                {
                    "$set": skeleton_movie_object
                },
                    upsert=True
                )
            )
    
    return ratings_operations, movie_operations
    

async def get_user_ratings(username, db_cursor=None, mongo_db=None, store_in_db=True, num_pages=None, return_unrated=False):
    url = "https://letterboxd.com/{}/films/by/date/page/{}/"
    
    if not num_pages:
        # Find them in the MongoDB database and grab the number of ratings pages
        user = db_cursor.find_one({"username": username})
        num_pages = user["num_ratings_pages"]

    # Fetch all responses within one Client session,
    # keep connection alive for all requests.
    async with ClientSession() as session:
        tasks = []
        # Make a request for each ratings page and add to task queue
        for i in range(num_pages):
            task = asyncio.ensure_future(fetch(url.format(username, i+1), session, {"username": username}))
            tasks.append(task)

        # Gather all ratings page responses
        scrape_responses = await asyncio.gather(*tasks)
        
    # Process each ratings page response, converting it into bulk upsert operations or output dicts
    tasks = []
    for response in scrape_responses:
        task = asyncio.ensure_future(generate_ratings_operations(response, send_to_db=store_in_db, return_unrated=return_unrated))
        tasks.append(task)
    
    parse_responses = await asyncio.gather(*tasks)

    if store_in_db == False:
        parse_responses = list(chain.from_iterable(parse_responses))[0]
        return parse_responses

    # Concatenate each response's upsert operations/output dicts
    upsert_ratings_operations = []
    upsert_movies_operations = []
    for response in parse_responses:
        upsert_ratings_operations += response[0]
        upsert_movies_operations += response[1]

    return upsert_ratings_operations, upsert_movies_operations


async def get_ratings(usernames, db_cursor=None, mongo_db=None, store_in_db=True):
    start = time.time()

    ratings_collection = mongo_db.ratings
    movies_collection = mongo_db.movies

    chunk_size = 16
    total_chunks = math.ceil(len(usernames) / chunk_size)

    pbar = tqdm(range(total_chunks))
    for chunk_index in pbar:
        tasks = []
        db_ratings_operations = []
        db_movies_operations = []

        start_index = chunk_size*chunk_index
        end_index = chunk_size*chunk_index + chunk_size
        username_chunk = usernames[start_index:end_index]

        pbar.set_description(f"Scraping ratings data for user group {chunk_index+1} of {total_chunks}")

        # For a given chunk, scrape each user's ratings and form an array of database upsert operations
        for i, username in enumerate(username_chunk):
            # print((chunk_size*chunk_index)+i, username)
            task = asyncio.ensure_future(get_user_ratings(username, db_cursor=db_cursor, mongo_db=mongo_db, store_in_db=store_in_db))
            tasks.append(task)

        # Gather all ratings page responses, concatenate all db upsert operatons for use in a bulk write
        user_responses = await asyncio.gather(*tasks)
        for response in user_responses:
            db_ratings_operations += response[0]
            db_movies_operations += response[1]

        if store_in_db:
            # Execute bulk upsert operations
            try:
                if len(db_ratings_operations) > 0:
                    # Bulk write all upsert operations into ratings collection in db
                    ratings_collection.bulk_write(db_ratings_operations, ordered=False)
                
                if len(db_movies_operations) > 0:
                    movies_collection.bulk_write(db_movies_operations, ordered=False)

            except BulkWriteError as bwe:
                pprint(bwe.details)
        
        # print_status(start, chunk_size, chunk_index, len(db_ratings_operations), len(usernames))


def print_status(start, chunk_size, chunk_index, total_operations, total_records):
    total_time = round((time.time() - start), 2)
    completed_records = (chunk_size*chunk_index)
    time_per_user = round(total_time / completed_records, 2)
    remaining_estimate = round(time_per_user * (total_records - completed_records), 2)

    print("\n================")
    print(f"Users Complete: {completed_records}")
    print(f"Users Remaining: {(total_records - completed_records)}")
    print("Chunk Database Operations:", total_operations)
    print()
    print("Current Time/User:", f"{time_per_user} seconds")
    print("Elapsed Time:", utils.format_seconds(total_time))
    print("Est. Time Remaining:", utils.format_seconds(remaining_estimate))
    print("================\n")


def main():
    import os
    if os.getcwd().endswith("data_processing"):
        from db_config import config
    else:
        from data_processing.db_config import config

    # Connect to MongoDB Client
    db_name = config["MONGO_DB"]

    if "CONNECTION_URL" in config.keys():
        client = pymongo.MongoClient(config["CONNECTION_URL"], server_api=pymongo.server_api.ServerApi('1'))
    else:
        # client = motor.motor_asyncio.AsyncIOMotorClient(f'mongodb+srv://{config["MONGO_USERNAME"]}:{config["MONGO_PASSWORD"]}@cluster0.{config["MONGO_CLUSTER_ID"]}.mongodb.net/?retryWrites=true&w=majority')
        client = pymongo.MongoClient(f'mongodb+srv://{config["MONGO_USERNAME"]}:{config["MONGO_PASSWORD"]}@cluster0.{config["MONGO_CLUSTER_ID"]}.mongodb.net/{db_name}?retryWrites=true&w=majority')

    # Find letterboxd database and user collection
    db = client[db_name]
    users = db.users
    all_users = users.find({})
    all_usernames = [x['username'] for x in all_users]

    loop = asyncio.get_event_loop()

    # Find number of ratings pages for each user and add to their Mongo document (note: max of 128 scrapable pages)
    # future = asyncio.ensure_future(get_page_counts(all_usernames, users))
    # future = asyncio.ensure_future(get_page_counts([], users))
    # loop.run_until_complete(future)

    # Find and store ratings for each user
    future = asyncio.ensure_future(get_ratings(all_usernames, users, db))
    # future = asyncio.ensure_future(get_ratings(["samlearner"], users, db))
    loop.run_until_complete(future)


if __name__ == "__main__":
    main()