# Rick's Pub Finder

## Run locally

Install the dependencies with `pip install -r requirements.txt`, then start the
app with `streamlit run streamlit_app.py`.

By default, local photos are read from the `photos` folder alongside the app.
Set `PUB_PHOTO_FOLDER` to use a different local folder.

## Use a public Google Drive photo folder

The app can list photos in a public Drive folder and match them to visits by
filename. Put image files directly in the folder (not in subfolders) and share
the folder using **Anyone with the link — Viewer**. Every photo in that folder
will therefore be publicly accessible.

Photo filenames must use this format:

`date pub name - locality.extension`

For example: `14-06-2024 The Crown - York.jpg`. Supported date formats include
`14-06-2024`, `140624`, and `14062024`; supported image types are JPG, PNG, GIF,
and WebP.

1. In Google Cloud Console, create a project, enable the Google Drive API, and
   create an API key. Restrict the key to the Google Drive API.
2. Copy the folder ID from the folder URL. It is the value after `/folders/`.
3. For local testing, create `.streamlit/secrets.toml` with:

   ```toml
   [google_drive]
   folder_id = "YOUR_FOLDER_ID"
   api_key = "YOUR_API_KEY"
   ```

4. Keep `secrets.toml` private. It is excluded from Git by `.gitignore`.

The app caches the Drive file listing for one hour. It fetches resized
thumbnails on demand and caches each thumbnail for one day; it does not download
the full photo collection to the app server.

## Deploy on Streamlit Community Cloud

Push the app and `requirements.txt` to a GitHub repository, create an app at
Streamlit Community Cloud, and select `streamlit_app.py` as the entry point.
In the app's **Settings → Secrets**, add the same TOML block shown above.
Because both the app and photo folder are public, anyone with the app URL can
view the visit data and photos.
