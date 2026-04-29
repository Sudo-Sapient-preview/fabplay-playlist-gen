"""
day_part_templates.py — Hardcoded day-part templates for all 9 business categories.

Used by:
  - sound_board.py  (included in Call 2 system prompt as context)
  - rag_retriever.py (character/genre fallback when building query embed text)
  - playlist_gen.py  (passed to orchestration layer)

Structure per day-part:
    name, start_time, end_time, character
"""

DAY_PART_TEMPLATES: dict[str, list[dict]] = {

    "cafe": [
        {
            "name": "Early Morning",
            "start_time": "06:00",
            "end_time": "09:00",
            "character": "Gentle, acoustic, low energy — easing customers into the day",
        },
        {
            "name": "Mid Morning",
            "start_time": "09:00",
            "end_time": "12:00",
            "character": "Warm, upbeat — productive coffee shop atmosphere",
        },
        {
            "name": "Lunch Rush",
            "start_time": "12:00",
            "end_time": "14:00",
            "character": "Higher energy, rhythmic — move the queue, maintain buzz",
        },
        {
            "name": "Afternoon",
            "start_time": "14:00",
            "end_time": "18:00",
            "character": "Relaxed, mellow — post-lunch wind-down, study crowd",
        },
        {
            "name": "Evening",
            "start_time": "18:00",
            "end_time": "22:00",
            "character": "Cosy, warm, slightly more ambient — end of day",
        },
    ],

    "qsr": [
        {
            "name": "Morning Rush",
            "start_time": "07:00",
            "end_time": "10:00",
            "character": "Energetic, familiar, quick-tempo — fast service feel",
        },
        {
            "name": "Mid Morning",
            "start_time": "10:00",
            "end_time": "12:00",
            "character": "Moderate, upbeat pop — slower traffic period",
        },
        {
            "name": "Lunch Peak",
            "start_time": "12:00",
            "end_time": "14:30",
            "character": "High energy, danceability up — maximize throughput",
        },
        {
            "name": "Afternoon",
            "start_time": "14:30",
            "end_time": "17:00",
            "character": "Mellowed, lighter — bridge before dinner rush",
        },
        {
            "name": "Dinner Rush",
            "start_time": "17:00",
            "end_time": "21:00",
            "character": "Re-energized, familiar pop — evening crowd",
        },
    ],

    "jewelry": [
        {
            "name": "Opening",
            "start_time": "10:00",
            "end_time": "13:00",
            "character": "Elegant, instrumental, low tempo — luxury feel on entry",
        },
        {
            "name": "Afternoon",
            "start_time": "13:00",
            "end_time": "17:00",
            "character": "Sophisticated, slightly warmer — browsing peak",
        },
        {
            "name": "Closing",
            "start_time": "17:00",
            "end_time": "20:00",
            "character": "Refined, calm — high-consideration purchase environment",
        },
    ],

    "fashion": [
        {
            "name": "Morning",
            "start_time": "10:00",
            "end_time": "13:00",
            "character": "Fresh, on-trend, moderate energy — setting the tone",
        },
        {
            "name": "Afternoon",
            "start_time": "13:00",
            "end_time": "17:00",
            "character": "Peak shopping, upbeat pop/dance — drive dwell time",
        },
        {
            "name": "Evening",
            "start_time": "17:00",
            "end_time": "20:00",
            "character": "Energetic, trendy — after-work shoppers",
        },
        {
            "name": "Late",
            "start_time": "20:00",
            "end_time": "22:00",
            "character": "Cooler, chill — closing wind-down",
        },
    ],

    "fine_dine": [
        {
            "name": "Lunch Service",
            "start_time": "12:00",
            "end_time": "15:00",
            "character": "Sophisticated, instrumental jazz/classical — business dining",
        },
        {
            "name": "Dinner Prep",
            "start_time": "17:00",
            "end_time": "19:00",
            "character": "Ambient, understated — transition to evening mood",
        },
        {
            "name": "Dinner Service",
            "start_time": "19:00",
            "end_time": "23:00",
            "character": "Intimate, warm, low tempo — fine dining experience",
        },
    ],

    "supermarket": [
        {
            "name": "Morning",
            "start_time": "08:00",
            "end_time": "12:00",
            "character": "Familiar, cheerful, moderate tempo — morning shop",
        },
        {
            "name": "Lunch",
            "start_time": "12:00",
            "end_time": "14:00",
            "character": "Upbeat, familiar pop — peak footfall",
        },
        {
            "name": "Afternoon",
            "start_time": "14:00",
            "end_time": "18:00",
            "character": "Easy listening, mixed genres — steady traffic",
        },
        {
            "name": "Evening",
            "start_time": "18:00",
            "end_time": "21:00",
            "character": "Slightly energetic — after-work rush",
        },
    ],

    "hotel": [
        {
            "name": "Morning",
            "start_time": "07:00",
            "end_time": "10:00",
            "character": "Relaxed, warm, acoustic — breakfast ambience",
        },
        {
            "name": "Day",
            "start_time": "10:00",
            "end_time": "13:00",
            "character": "Upscale, ambient — lobby / check-in flow",
        },
        {
            "name": "Afternoon",
            "start_time": "13:00",
            "end_time": "17:00",
            "character": "Sophisticated lounge — leisure guests",
        },
        {
            "name": "Evening",
            "start_time": "17:00",
            "end_time": "21:00",
            "character": "Cocktail hour — higher valence, warm jazz or lounge",
        },
        {
            "name": "Night",
            "start_time": "21:00",
            "end_time": "00:00",
            "character": "Calm, intimate — restaurant/bar closing",
        },
    ],

    "furniture": [
        {
            "name": "Morning",
            "start_time": "10:00",
            "end_time": "13:00",
            "character": "Calm, Scandinavian-aesthetic, low tempo — mindful browsing",
        },
        {
            "name": "Afternoon",
            "start_time": "13:00",
            "end_time": "17:00",
            "character": "Warm, comfortable — family browsing peak",
        },
        {
            "name": "Evening",
            "start_time": "17:00",
            "end_time": "20:00",
            "character": "Relaxed, melodic — closing hour",
        },
    ],

    "mall": [
        {
            "name": "Morning",
            "start_time": "10:00",
            "end_time": "13:00",
            "character": "Upbeat, mainstream pop — setting shopper mood",
        },
        {
            "name": "Afternoon Peak",
            "start_time": "13:00",
            "end_time": "17:00",
            "character": "High energy, diverse genres — maximum footfall",
        },
        {
            "name": "Evening",
            "start_time": "17:00",
            "end_time": "20:00",
            "character": "Energetic, youthful — after-school/work crowd",
        },
        {
            "name": "Closing",
            "start_time": "20:00",
            "end_time": "22:00",
            "character": "Mellowed — wind-down, encourage final purchases",
        },
    ],

    "casual_dine": [
        {
            "name": "Lunch",
            "start_time": "11:00",
            "end_time": "14:00",
            "character": "Upbeat, friendly, moderate tempo — relaxed lunch crowd",
        },
        {
            "name": "Afternoon",
            "start_time": "14:00",
            "end_time": "17:00",
            "character": "Easy listening, light pop — quieter between-meal period",
        },
        {
            "name": "Dinner",
            "start_time": "17:00",
            "end_time": "22:00",
            "character": "Warm, social, moderate energy — evening dining crowd",
        },
    ],

    "spa": [
        {
            "name": "Morning",
            "start_time": "09:00",
            "end_time": "12:00",
            "character": "Calm, meditative, ambient — morning treatment sessions",
        },
        {
            "name": "Afternoon",
            "start_time": "12:00",
            "end_time": "17:00",
            "character": "Deeply relaxing, instrumental — peak treatment hours",
        },
        {
            "name": "Evening",
            "start_time": "17:00",
            "end_time": "21:00",
            "character": "Gentle, serene, acoustic — wind-down after-work guests",
        },
    ],

    "gym": [
        {
            "name": "Early Morning",
            "start_time": "06:00",
            "end_time": "09:00",
            "character": "High energy, fast tempo, motivating — pre-work workout crowd",
        },
        {
            "name": "Mid Morning",
            "start_time": "09:00",
            "end_time": "12:00",
            "character": "Steady high energy, electronic/hip-hop — regular morning members",
        },
        {
            "name": "Afternoon",
            "start_time": "12:00",
            "end_time": "17:00",
            "character": "Energetic, upbeat — lunchtime and afternoon workouts",
        },
        {
            "name": "Evening Peak",
            "start_time": "17:00",
            "end_time": "21:00",
            "character": "Maximum energy, driving beats — after-work rush hour",
        },
    ],

    "salon": [
        {
            "name": "Morning",
            "start_time": "09:00",
            "end_time": "12:00",
            "character": "Relaxed, friendly pop — easy morning appointments",
        },
        {
            "name": "Afternoon",
            "start_time": "12:00",
            "end_time": "17:00",
            "character": "Upbeat, trendy — busy afternoon bookings",
        },
        {
            "name": "Evening",
            "start_time": "17:00",
            "end_time": "20:00",
            "character": "Warm, conversational energy — after-work appointments",
        },
    ],

    "bookstore": [
        {
            "name": "Morning",
            "start_time": "09:00",
            "end_time": "13:00",
            "character": "Calm, instrumental, low tempo — quiet browsing atmosphere",
        },
        {
            "name": "Afternoon",
            "start_time": "13:00",
            "end_time": "18:00",
            "character": "Soft, acoustic, intellectual — peak browsing hours",
        },
        {
            "name": "Evening",
            "start_time": "18:00",
            "end_time": "21:00",
            "character": "Mellow, warm — evening readers and events",
        },
    ],

    "electronics": [
        {
            "name": "Morning",
            "start_time": "10:00",
            "end_time": "13:00",
            "character": "Modern, electronic, moderate energy — opening hours",
        },
        {
            "name": "Afternoon",
            "start_time": "13:00",
            "end_time": "18:00",
            "character": "Upbeat, tech-forward, energetic — peak sales hours",
        },
        {
            "name": "Evening",
            "start_time": "18:00",
            "end_time": "21:00",
            "character": "Sleek, contemporary — after-work shoppers",
        },
    ],

    "kids_store": [
        {
            "name": "Morning",
            "start_time": "10:00",
            "end_time": "13:00",
            "character": "Playful, cheerful, upbeat — families with young children",
        },
        {
            "name": "Afternoon",
            "start_time": "13:00",
            "end_time": "18:00",
            "character": "Fun, energetic, positive — peak family shopping hours",
        },
        {
            "name": "Evening",
            "start_time": "18:00",
            "end_time": "20:00",
            "character": "Lively but winding down — last shoppers of the day",
        },
    ],

    "bar_lounge": [
        {
            "name": "Happy Hour",
            "start_time": "16:00",
            "end_time": "19:00",
            "character": "Upbeat, social, moderate energy — after-work crowd arrives",
        },
        {
            "name": "Evening",
            "start_time": "19:00",
            "end_time": "22:00",
            "character": "Warm, jazz/lounge, conversational — dinner and drinks crowd",
        },
        {
            "name": "Late Night",
            "start_time": "22:00",
            "end_time": "01:00",
            "character": "Higher energy, danceable — late-night crowd builds",
        },
    ],

    "clinic": [
        {
            "name": "Morning",
            "start_time": "08:00",
            "end_time": "12:00",
            "character": "Calm, reassuring, soft — patients in waiting area",
        },
        {
            "name": "Afternoon",
            "start_time": "12:00",
            "end_time": "17:00",
            "character": "Gentle, ambient, low stress — steady appointment flow",
        },
        {
            "name": "Evening",
            "start_time": "17:00",
            "end_time": "20:00",
            "character": "Soft, soothing — end-of-day patients",
        },
    ],

    "coworking": [
        {
            "name": "Morning",
            "start_time": "08:00",
            "end_time": "12:00",
            "character": "Focused, lo-fi, moderate energy — deep work hours",
        },
        {
            "name": "Afternoon",
            "start_time": "12:00",
            "end_time": "17:00",
            "character": "Productive, steady tempo — collaborative afternoon sessions",
        },
        {
            "name": "Evening",
            "start_time": "17:00",
            "end_time": "21:00",
            "character": "Relaxed, ambient — late workers and evening members",
        },
    ],

    "footwear": [
        {
            "name": "Morning",
            "start_time": "10:00",
            "end_time": "13:00",
            "character": "Energetic, on-trend, moderate tempo — opening hours, early browsers",
        },
        {
            "name": "Afternoon",
            "start_time": "13:00",
            "end_time": "17:00",
            "character": "Upbeat, rhythmic, peak dwell time — high footfall shopping hours",
        },
        {
            "name": "Evening",
            "start_time": "17:00",
            "end_time": "20:00",
            "character": "Energetic, youthful, trendy — after-work and evening shoppers",
        },
        {
            "name": "Closing",
            "start_time": "20:00",
            "end_time": "22:00",
            "character": "Mellowed, cool — wind-down, last purchases of the day",
        },
    ],

}


def get_template(category: str) -> list[dict]:
    """Return day-part template list for the given business category."""
    return DAY_PART_TEMPLATES.get(category, [])


def get_day_part_hours(day_part: dict) -> float:
    """
    Compute duration in hours from start_time and end_time strings (HH:MM).
    Handles midnight crossover (e.g. 21:00 → 00:00 = 3 hours).
    """
    def to_minutes(t: str) -> int:
        h, m = map(int, t.split(":"))
        return h * 60 + m

    start = to_minutes(day_part["start_time"])
    end   = to_minutes(day_part["end_time"])
    if end == 0:          # midnight represented as "00:00"
        end = 24 * 60
    if end <= start:      # crosses midnight
        end += 24 * 60
    return (end - start) / 60.0


def target_track_count(day_part: dict) -> int:
    """
    Return target number of tracks for a day-part based on its duration.
      < 2 hours  → 40 tracks
      2–3 hours  → 55 tracks
      > 3 hours  → 75 tracks
    """
    hours = get_day_part_hours(day_part)
    if hours < 2:
        return 40
    elif hours <= 3:
        return 55
    else:
        return 75
