# Lost Pet Tracker
This is our group project for COSC 612.
It is a web application designed to aid in finding lost pets.

Team Members: Benjamin Biscomb, Nimra Shakoor, Amari Harrington, Khasim Raza

# Dependencies
- everything in `back-end/requirements.txt`
- a PostgreSQL database set using the schema in `lost-pet-database.sql`
- Python 3.x
- node.js 24.x

# HOW TO RUN
1. edit `config.ini` in back-end as needed

2. from `back-end`, run:

   ```
   pip install -r requirements.txt
   python database.py
   python server.py
   ```

3. open the site in browser, using the port specified in `config.ini`

# Notes
- AI matching and SMTP are not currently set up.
- Verification codes currently appear in the terminal running the server after post creation.
