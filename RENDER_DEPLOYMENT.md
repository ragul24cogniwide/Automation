# Deploying the Personal Agent Backend to Render

This FastAPI backend is fully prepared for hosting on [Render](https://render.com). It connects directly to your Neon PostgreSQL database and serves webhooks for Google Apps Script and your React Native mobile app.

---

## Option 1: 1-Click Blueprint (Recommended)

1. Push this repository to **GitHub** or **GitLab**.
2. Go to [https://dashboard.render.com](https://dashboard.render.com).
3. Click **"New +"** -> **"Blueprint"**.
4. Select your repository.
5. Render will detect `render.yaml` automatically.
6. Provide your environment variables:
   - `DATABASE_URL`: `postgresql://neondb_owner:npg_wE7nFSK6tlYs@ep-lingering-lab-b5ft0ytc-pooler.c-7.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require`
   - `GEMINI_API_KEY`: Your Google Gemini API Key
7. Click **"Apply"**. Render will build and deploy your service.

---

## Option 2: Manual Web Service Setup

If you prefer to configure the Web Service manually:

1. In Render Dashboard, click **"New +"** -> **"Web Service"**.
2. Connect your Git repository.
3. Choose the **Root Directory**: `agent-backend` (or leave blank if the repository root is the backend).
4. Configure settings:
   - **Language / Runtime**: `Python`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Health Check Path**: `/api/health`
5. Under **Environment Variables**, add:
   - `DATABASE_URL`: `postgresql://neondb_owner:npg_wE7nFSK6tlYs@ep-lingering-lab-b5ft0ytc-pooler.c-7.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require`
   - `GEMINI_API_KEY`: `your_gemini_api_key`
   - `PYTHON_VERSION`: `3.11.8`
6. Click **"Create Web Service"**.

---

## After Deployment

Once deployed, Render gives you a public HTTPS URL (for example: `https://personal-agent-backend.onrender.com`).

1. **Verify deployment**:
   Open `https://your-app.onrender.com/api/health` in your browser.
   It should return:
   ```json
   {
     "status": "online",
     "database": "connected",
     "gemini_configured": true
   }
   ```

2. **Connect Google Apps Script**:
   - Open your script at [script.google.com](https://script.google.com).
   - In `Code.gs`, set:
     ```javascript
     const BACKEND_URL = "https://your-app.onrender.com";
     ```
   - Run `testConnection()`.

3. **Connect React Native Mobile App**:
   - In `AgentMobile/src/config.ts`, set:
     ```typescript
     export const CONFIG = {
       BACKEND_URL: 'https://your-app.onrender.com',
     };
     ```
   - Or change the URL directly in the app's UI!
