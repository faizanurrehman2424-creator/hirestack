import os
import json
import pandas as pd
from flask import Flask, request, jsonify, send_file, render_template
from werkzeug.utils import secure_filename
from pypdf import PdfReader
from fpdf import FPDF
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import requests
import re
import time

app = Flask(__name__)

# --- CONFIGURATION ---
UPLOAD_FOLDER = 'uploads'
STATIC_FOLDER = 'static'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(STATIC_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

GENAI_API_KEY = os.environ.get("GENAI_API_KEY")
if GENAI_API_KEY:
    genai.configure(api_key=GENAI_API_KEY)

try:
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    client_gs = gspread.authorize(creds)
    sheet = client_gs.open("Hirestacks Jobs").sheet1
except Exception as e:
    print(f"Warning: Google Sheets not connected. Error: {e}")
    sheet = None

def clean_text(text):
    if not text: return ""
    text = str(text)
    replacements = {
        '\u2013': '-', '\u2014': '-', '\u2018': "'", '\u2019': "'",
        '\u201c': '"', '\u201d': '"', '\u2022': '*', '\u2026': '...',
    }
    for char, repl in replacements.items():
        text = text.replace(char, repl)
    return text.encode('latin-1', 'replace').decode('latin-1')

class Round8_PDF(FPDF):
    def header(self):
        # Use logos.png or logo.png depending on your actual filename
        logo_path = os.path.join(app.root_path, 'static', 'logos.png') 
        
        if os.path.exists(logo_path):
            # --- UPDATED: Smaller logo and Top-Left placement ---
            # Reduced w from 40 to 30 for a more professional, smaller look
            self.image(logo_path, x=10, y=10, w=30) 
            
        # --- UPDATED: Increased Spacing ---
        # Increased to 35. This ensures the cursor moves down far enough 
        # so that the heading (which is printed next) cannot touch the logo.
        self.ln(35) 

    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f'Page {self.page_no()}', 0, 0, 'C')

    def section_header(self, title):
        self.ln(5)
        self.set_font('Arial', 'B', 12)
        self.set_text_color(0, 0, 0)
        self.cell(0, 8, clean_text(title).upper(), 0, 1, 'L')
        y = self.get_y()
        self.set_draw_color(0, 0, 0)
        self.line(10, y, 200, y) 
        self.ln(3)
        
def extract_text_from_pdf(pdf_path):
    reader = PdfReader(pdf_path)
    text = ""
    for page in reader.pages:
        extracted = page.extract_text()
        if extracted:
            text += extracted + "\n"
    return text

def ai_process_cv(text):
    print(f"--- 1. PDF TEXT LENGTH: {len(text)} ---")
    try:
        model = genai.GenerativeModel('gemini-flash-latest')
        prompt = f"""
        You are an expert Headhunter.
        Input CV Text: {text}
        TASKS:
        1. Identify Real Name.
        2. ANONYMIZE content.
        3. Identify Seniority.
        4. Generate Search Data.
        RETURN JSON ONLY:
        {{
            "structured_cv": {{
                "role_title": "Role", "summary": "...", "skills": ["..."], 
                "languages": ["..."],
                "experience": [ {{ "title": "...", "company": "...", "dates": "...", "description": "..." }} ],
                "education": [ {{ "degree": "...", "school": "...", "dates": "..." }} ]
            }},
            "real_name": "Name",
            "job_titles": ["Title1"],
            "keywords_to_avoid": ["..."]
        }}
        """
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        try:
            data = json.loads(response.text)
        except:
            safe_text = response.text.strip()
            if not safe_text.endswith("}"): safe_text += '}'
            data = json.loads(safe_text)
        return data
    except Exception as e:
        print(f"!!! AI ERROR: {e} !!!")
        return {"real_name": "Error", "job_titles": ["Developer"], "structured_cv": {}}

def get_ai_scores(jobs, skills_list):
    if not jobs or not skills_list: return jobs
    jobs_to_score = jobs[:10]
    job_text_block = ""
    for idx, job in enumerate(jobs_to_score):
        job_text_block += f"ID {idx}: {job['title']} at {job['company']} - {job['description']}\n"

    prompt = f"""
    Candidate Skills: {", ".join(skills_list)}
    Jobs: {job_text_block}
    Rate jobs (0-100) on relevance. Provide 1 sentence reason.
    RETURN JSON: {{ "scores": [ {{ "id": 0, "score": 95, "reason": "..." }} ] }}
    """
    model = genai.GenerativeModel('gemini-flash-latest')
    try:
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        score_data = json.loads(response.text)
        for item in score_data.get("scores", []):
            idx = item.get("id")
            if idx is not None and idx < len(jobs_to_score):
                jobs[idx]["match_score"] = item.get("score")
                jobs[idx]["match_reason"] = item.get("reason")
    except Exception as e:
        print(f"Scoring Error: {e}")
    return jobs

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/anonymize', methods=['POST'])
def anonymize():
    if 'file' not in request.files: return jsonify({"error": "No file"}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "No file"}), 400
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(file.filename))
    file.save(filepath)
    try:
        raw_text = extract_text_from_pdf(filepath)
        ai_result = ai_process_cv(raw_text)
        return jsonify(ai_result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/download_pdf', methods=['POST'])
def download_pdf():
    data = request.json
    cv_data = data.get('structured_cv', {})
    pdf = Round8_PDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.set_font("Arial", "B", 16)
    pdf.set_text_color(0, 0, 0)
    role_title = clean_text(cv_data.get('role_title', 'CANDIDATE PROFILE'))
    pdf.cell(0, 10, role_title, 0, 1, 'L')
    
    pdf.set_font("Arial", "I", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, "Anonymized Curriculum Vitae", 0, 1, 'L')
    pdf.ln(5)

    if cv_data.get('summary'):
        pdf.section_header("Professional Summary")
        pdf.section_body(cv_data.get('summary'))

    if cv_data.get('skills'):
        pdf.section_header("Skills")
        skills_str = ", ".join(cv_data.get('skills', []))
        pdf.section_body(skills_str)

    if cv_data.get('experience'):
        pdf.section_header("Professional Experience")
        for job in cv_data['experience']:
            pdf.set_font("Arial", "B", 10)
            pdf.cell(0, 5, f"{clean_text(job.get('title',''))} | {clean_text(job.get('company',''))}", 0, 1)
            pdf.set_font("Arial", "I", 9)
            pdf.set_text_color(80, 80, 80)
            pdf.cell(0, 5, clean_text(job.get('dates','')), 0, 1)
            pdf.set_text_color(0, 0, 0)
            pdf.section_body(job.get('description', ''))
            pdf.ln(2)

    if cv_data.get('education'):
        pdf.section_header("Education")
        for edu in cv_data['education']:
            pdf.set_font("Arial", "B", 10)
            pdf.cell(0, 5, clean_text(edu.get('school','')), 0, 1)
            pdf.set_font("Arial", "", 10)
            pdf.cell(0, 5, f"{clean_text(edu.get('degree',''))} | {clean_text(edu.get('dates',''))}", 0, 1)
            pdf.ln(2)

    if cv_data.get('languages'):
        pdf.section_header("Languages")
        langs = ", ".join(cv_data.get('languages', []))
        pdf.section_body(langs)

    output_path = os.path.join(app.config['UPLOAD_FOLDER'], 'anonymous_cv.pdf')
    pdf.output(output_path)
    return send_file(output_path, as_attachment=True)

@app.route('/search_jobs', methods=['POST'])
def search_jobs():
    data = request.json
    raw_titles = data.get('job_titles', [])
    candidate_name = data.get('real_name', 'Unknown')
    candidate_skills = data.get('cv_skills', []) 
    
    # --- PERFORMANCE FIX: Only use 1 query to prevent timeout ---
    search_queries = []
    if raw_titles:
        # Just grab the first title, clean it up, and use that.
        clean_t = re.sub(r'\(.*?\)', '', raw_titles[0]).replace('/', ' ').strip()
        search_queries.append(clean_t)

    JOOBLE_KEY = os.environ.get("JOOBLE_KEY")
    API_URL = "https://jooble.org/api/" + JOOBLE_KEY
    raw_results = []
    seen_urls = set()

    for title in search_queries:
        if len(raw_results) >= 15: break # Reduced limit
        payload = { "keywords": title, "location": "United States", "page": 1 }

        try:
            response = requests.post(API_URL, json=payload, timeout=5) # 5s timeout for API
            if response.status_code == 200:
                data = response.json()
                jobs = data.get('jobs', [])
                if jobs:
                    for j in jobs:
                        if j.get('link') not in seen_urls:
                            raw_results.append(j)
                            seen_urls.add(j.get('link'))
        except Exception as e:
            print(f"Jooble Error: {e}")
            continue

    forbidden_words = ["recruitment", "recruiter", "talent acquisition", "hr manager", "headhunter", "internship"]
    final_jobs = []
    
    for job in raw_results:
        title = job.get('title', '').lower()
        company = job.get('company', '').lower()
        if any(bad in title for bad in forbidden_words): continue
        if any(bad in company for bad in forbidden_words): continue
        
        final_jobs.append({
            "title": job.get('title'),
            "company": job.get('company', 'Unknown'),
            "location": job.get('location', 'United States'),
            "job_url": job.get('link'),
            "description": job.get('snippet', ''),
            "match_score": 0,
            "match_reason": "Analyzing..."
        })

    final_jobs = final_jobs[:10]
    if candidate_skills:
        final_jobs = get_ai_scores(final_jobs, candidate_skills)

    if sheet and final_jobs:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for job in final_jobs:
            try:
                sheet.append_row([
                    timestamp, candidate_name, job['title'], job['company'], 
                    job['location'], job['job_url'], job.get('match_score', 0)
                ])
            except Exception as e:
                print(f"Sheet Error: {e}")
    
    return jsonify(final_jobs)

@app.route('/generate_csv', methods=['POST'])
def generate_csv():
    data = request.json
    jobs = data.get('jobs', [])
    if not jobs: return jsonify({"error": "No jobs"}), 400
    df = pd.DataFrame(jobs)
    csv_path = os.path.join(app.config['UPLOAD_FOLDER'], 'found_jobs.csv')
    df.to_csv(csv_path, index=False)
    return send_file(csv_path, as_attachment=True)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
