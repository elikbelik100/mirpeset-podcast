# המרפסת – Podcast bot

Turns the recordings in the המרפסת app into a podcast on Spotify (and Apple Podcasts,
or any other podcast app), and updates it automatically every Sunday.

**How it works:** the bot reads the app's recordings list and lesson details, tags each
recording (lecturer, topic, occasion, series), downloads only *new* recordings from Google
Drive, shrinks them to speech quality (about 30 MB per hour), and publishes a podcast feed.
Spotify checks the feed regularly and adds new episodes by itself. Nobody uploads anything by hand.

```
app (recordings.json + Supabase) ──► bot (GitHub Actions, weekly) ──► feed.xml (GitHub Pages) ──► Spotify
                                         │
                                         └─► audio files (GitHub Releases)
```

## One-time setup (about 20 minutes)

1. **Create the repository.** On GitHub: *New repository* → name `mirpeset-podcast` →
   **Public** (needed so Spotify can read the feed) → *Create*. Upload all the files in this
   folder, including the hidden `.github` folder.
2. **Add the database key.** Repository → *Settings → Secrets and variables → Actions →
   New repository secret*:
   - Name `SUPABASE_KEY`, value: the **anon public** key from Supabase
     (*Project Settings → API*). It's the same public key the app already uses.
   - *(Optional)* `GOOGLE_API_KEY`: a Google Cloud API key with the Drive API enabled.
     Only needed if Drive starts blocking downloads.
3. **Turn on the website.** *Settings → Pages → Build and deployment*: Source **Deploy from a
   branch**, branch **main**, folder **/docs** → *Save*.
4. **First run.** *Actions → Update podcast → Run workflow*. The first run processes the whole
   archive (about 160 recordings), which takes 2–3 hours. If it stops at the time limit, run it
   again; it picks up where it left off.
5. **Check the result.**
   - Episode page with filters: `https://<your-username>.github.io/mirpeset-podcast/`
   - Feed: `https://<your-username>.github.io/mirpeset-podcast/feed.xml`
   - Tagging report: `reports/review.md` in the repository. Fix anything odd in `tags.yml`.
6. **Submit to Spotify (once).** <https://creators.spotify.com> → *Get started* →
   *I have a podcast* → *Somewhere else* → paste the feed URL → Spotify emails a code to
   `torabmirpeset@gmail.com` → enter it → *Submit*. Usually live within a few hours.
   Then put the show's Spotify link in `config.yml` under `show:` as `spotify_url: "..."`,
   so the episode page links to it.

## Weekly operation

Nothing to do. Every Sunday the bot adds any new recordings from the app. If something fails
(e.g. a Drive file isn't shared publicly), it's listed in `reports/review.md` and retried next week.

## Fixing tags · תיקון תיוגים

הכל בקובץ **`tags.yml`**. עורכים אותו ישירות באתר של GitHub (אייקון העיפרון). אחרי שמירה הבוט
מתייג מחדש את כל הפרקים תוך כמה דקות, בלי להעלות שום קובץ שמע מחדש.

- **מרצה מופיע בשני כתיבים:** מוסיפים את הכתיב השגוי לרשימה של הכתיב הנכון תחת `rabbis`.
- **מרצה חדש:** מוסיפים שורה `"הרב פלוני אלמוני": []`.
- **חג/מועד לא זוהה:** מוסיפים מילת מפתח לרשימת `match` של המועד ב-`occasions`.
- **סדרה חדשה:** סדרות בשם "X, מפגש N" או "סדרה בנושא X" מזוהות לבד. לאחרות מוסיפים ל-`series`.
- **הכי טוב לתיוג:** למלא באפליקציה את שם המרצה ואת כותרת השיעור. מה שחסר שם, הבוט משלים
  משם ההקלטה ומשם הקובץ בדרייב.

`reports/review.md` מתעדכן בכל ריצה ומראה מה כדאי לתקן: מרצים לא מוכרים, הקלטות בלי מרצה,
הקלטות בלי נושא, והורדות שנכשלו.

## Files

| File | What it is |
|---|---|
| `config.yml` | Show name, description, email, cover, audio quality |
| `tags.yml` | Tagging rules: lecturers, occasions, series, programs |
| `bot/build.py` | The bot |
| `state/episodes.json` | What has been processed (don't edit) |
| `docs/` | Published feed, episode page, cover (generated) |
| `reports/review.md` | Tagging report (generated) |

**Important:** never delete `state/episodes.json` or the `audio-*` Releases. Episode IDs in the
feed are permanent; Spotify uses them to track episodes and listener progress.

Local test without uploading anything: `python bot/build.py --dry-run`
