"""
Field maps for erail's `~`-delimited payloads.

These are NOT guesses. The index constants, class order and quota order are
lifted verbatim from erail's own client bundle:

    https://erail.in/js5/cmp/eRailMobileHTMLEN.js

which declares `T_No=0, T_Name=1, ...`, `n_Classes2="1A 2A 3A ..."` and
`d_Quota="GN TQ PT LD SS FT HP YU DP PH"`. Everything here was then verified
against 155 real records captured from the live endpoints.
"""

# --- getTrains.aspx record: 72 named fields, one record per `^` segment ---
# Identical layout whether the query used Station_From/Station_To or TrainNo.
T = {
    "No": 0,
    "Name": 1,
    "StnFirstName": 2,
    "StnFirst": 3,
    "StnLastName": 4,
    "StnLast": 5,
    "StnFromName": 6,
    "StnFrom": 7,
    "StnToName": 8,
    "StnTo": 9,
    "Departure": 10,
    "Arrival": 11,
    "Travel": 12,
    "RunDays": 13,
    "Mon": 14,
    "Tues": 15,
    "Wed": 16,
    "Thur": 17,
    "Fri": 18,
    "Sat": 19,
    "Sun": 20,
    "Classes": 21,
    "StnFromHalt": 22,
    "StnToHalt": 23,
    "LHB": 24,
    "StnFromSNo": 25,
    "DepartureFirstStn": 26,
    "ArrivalLastStn": 27,
    "StnToSNo": 28,
    "RunDaysFirstStn": 29,
    "Arrival_AtFrom": 30,
    "DepartureAtTo": 31,
    "TrainType": 32,
    "TrainID": 33,
    "RunningDayToStn": 34,
    "Pantry": 35,
    "DataSource": 36,
    "DateFrom": 37,
    "DateTo": 38,
    "Distance": 39,
    "AvSpeed": 40,
    "Fare": 41,
    "IsDuronto": 42,
    "IsGaribRath": 43,
    "Note": 44,
    "ARP": 45,
    "RunningDayFromStn": 46,
    "TrainNoAlias": 47,
    "AdminNote": 48,
    "After": 49,
    "TrainType2": 50,
    "WaitTotal": 51,
    "Status": 52,
    "OwningRailway": 53,
    "Division": 54,
    "Guage": 55,
    "RakeShare": 56,
    "Reverse": 57,
    "Flags": 58,
    "Rakes": 59,
    "IsSpecial": 60,
    "RouteID": 61,
    "MaxSeats": 62,
    "DepartureDate": 63,
    "ArrivalDate": 64,
    "WaitTip": 65,
    "NextDepartureDate": 66,
    "RakesList": 67,
    "NextArrivalAtFrom": 68,
    "PFFrom": 69,
    "PFTo": 70,
    # erail declares T_ID=71 but overwrites that slot at runtime with its own
    # row index. In the raw payload, 71 holds the long train name.
    "LongName": 71,
}

# Positional index for the T_Classes bitmask (field 21) and for the 15 fare
# groups inside T_Fare (field 41). erail's mask is 15 chars; only 14 are used.
CLASSES = ["1A", "2A", "3A", "CC", "FC", "SL", "2S", "3E", "GN", "EA", "EC", "EV", "VC", "VS"]

CLASS_NAMES = {
    "1A": "AC First Class",
    "2A": "AC 2 Tier",
    "3A": "AC 3 Tier",
    "3E": "AC 3 Tier Economy",
    "CC": "AC Chair Car",
    "EC": "Executive Chair Car",
    "EA": "Anubhuti Class",
    "EV": "Vistadome AC",
    "VC": "Vistadome Chair Car",
    "VS": "Vistadome Non-AC",
    "FC": "First Class",
    "SL": "Sleeper",
    "2S": "Second Sitting",
    "GN": "General / Unreserved",
}

# Positional index for the 10 colon-separated slots in T_MaxSeats (field 62)
# and for the availability matrix in the TRAINROUTE response.
QUOTAS = ["GN", "TQ", "PT", "LD", "SS", "FT", "HP", "YU", "DP", "PH"]

QUOTA_NAMES = {
    "GN": "General",
    "TQ": "Tatkal",
    "PT": "Premium Tatkal",
    "LD": "Ladies",
    "SS": "Senior Citizen",
    "FT": "Foreign Tourist",
    "HP": "Handicapped",
    "YU": "Yuva",
    "DP": "Duty Pass",
    "PH": "Parliament House",
}

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# --- data.aspx?Action=TRAINROUTE station row ---
R = {
    "SNo": 0,
    "Code": 1,
    "Name": 2,
    "Arrival": 3,          # "First" at the originating station
    "Departure": 4,        # "Last" at the terminating station
    "HaltMinutes": 5,
    "DistanceKm": 6,
    "Day": 7,
    "Unknown8": 8,
    "Unknown9": 9,
    "Zone": 10,
    "Division": 11,
    "NameHindi": 12,       # HTML numeric entities
    "QuotaType": 13,       # PQ = pooled quota, RL = remote location
    "Latitude": 14,
    "Longitude": 15,
    "MinutesToArrival": 16,   # elapsed minutes from the train's origin
    "MinutesToDeparture": 17,
}

# --- IRCTC cancellation minimum flat charges, per passenger, by class ---
# Source: IRCTC Refund Rules 2015, Rule 4 (confirmed tickets cancelled > 48h).
MIN_CANCELLATION_CHARGE = {
    "1A": 240, "EA": 240, "EC": 240,
    "2A": 200, "FC": 200,
    "3A": 180, "3E": 180, "CC": 180, "EV": 180, "VC": 180, "VS": 180,
    "SL": 120,
    "2S": 60,
}
DEFAULT_MIN_CANCELLATION_CHARGE = 120

# Flat clerkage on cancelling a waitlisted or RAC ticket, per passenger.
CLERKAGE_CHARGE = 60
