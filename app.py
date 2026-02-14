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

# 1. SETUP GEMINI API
GENAI_API_KEY = os.environ.get("GENAI_API_KEY")
if GENAI_API_KEY:
    genai.configure(api_key=GENAI_API_KEY)

# 2. SETUP GOOGLE SHEETS
try:
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    client_gs = gspread.authorize(creds)
    sheet = client_gs.open("Round8 Jobs").sheet1
except Exception as e:
    print(f"Warning: Google Sheets not connected. Error: {e}")
    sheet = None

# --- TEXT CLEANING HELPER (CRITICAL FIX) ---
def clean_text(text):
    """
    Replaces special Unicode characters that crash FPDF (like en-dashes)
    with safe ASCII alternatives.
    """
    if not text: return ""
    text = str(text)
    
    # specific replacements for common CV characters
    replacements = {
        '\u2013': '-',   # En-dash -> hyphen
        '\u2014': '-',   # Em-dash -> hyphen
        '\u2018': "'",   # Left single quote
        '\u2019': "'",   # Right single quote
        '\u201c': '"',   # Left double quote
        '\u201d': '"',   # Right double quote
        '\u2022': '*',   # Bullet point
        '\u2026': '...', # Ellipsis
    }
    for char, repl in replacements.items():
        text = text.replace(char, repl)
        
    # Final safety net: Force convert to latin-1, replacing unknown chars with '?'
    return text.encode('latin-1', 'replace').decode('latin-1')

# --- PDF GENERATOR CLASS ---
class Round8_PDF(FPDF):
    def header(self):
        logo_path = os.path.join(app.root_path, 'static', 'logo.png')
        if os.path.exists(logo_path):
            self.image(logo_path, x=10, y=10, w=40) 
        self.ln(25)

    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f'Page {self.page_no()}', 0, 0, 'C')

    def section_header(self, title):
        self.ln(5)
        self.set_font('Arial', 'B', 12)
        self.set_text_color(30, 58, 138)
        # Apply cleaning to header title too
        self.cell(0, 8, clean_text(title).upper(), 0, 1, 'L')
        
        x = self.get_x()
        y = self.get_y()
        self.set_draw_color(30, 58, 138)
        self.line(10, y, 200, y) 
        self.ln(3)

    def section_body(self, text):
        self.set_font('Arial', '', 10)
        self.set_text_color(0, 0, 0)
        # CRITICAL: Use clean_text here
        self.multi_cell(0, 5, clean_text(text))
        self.ln(3)

# --- HELPER FUNCTIONS ---

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
        2. ANONYMIZE content (remove phone, email, address).
        3. Identify Seniority.
        4. Generate Search Data (Job Titles & Avoid List).

        CRITICAL: Populate "structured_cv" first.

        RETURN JSON ONLY:
        {{
            "structured_cv": {{
                "role_title": "Anonymized Role",
                "summary": "Summary...",
                "skills": ["Skill1", "Skill2"],
                "languages": ["Lang1"],
                "experience": [
                    {{ "title": "Job Title", "company": "Company", "dates": "Dates", "description": "Details..." }}
                ],
                "education": [
                    {{ "degree": "Degree", "school": "School", "dates": "Dates" }}
                ]
            }},
            "real_name": "Name",
            "job_titles": ["Title1", "Title2"],
            "keywords_to_avoid": ["Avoid1", "Avoid2"]
        }}
        """
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            safe_text = response.text.strip()
            if not safe_text.endswith("}"): safe_text += '}'
            try: data = json.loads(safe_text)
            except: return {"real_name": "Unknown", "job_titles": ["Developer"], "keywords_to_avoid": [], "structured_cv": {"summary": "Error parsing."}}
            
        return data

    except Exception as e:
        print(f"!!! AI ERROR: {e} !!!")
        return {"real_name": "Error", "job_titles": ["Business Analyst"], "keywords_to_avoid": [], "structured_cv": {"summary": "Error processing file.", "skills": [], "experience": [], "education": []}}

def get_ai_scores(jobs, skills_list):
    if not jobs or not skills_list: return jobs
    jobs_to_score = jobs[:10]
    
    job_text_block = ""
    for idx, job in enumerate(jobs_to_score):
        job_text_block += f"ID {idx}: {job['title']} at {job['company']} - {job['description']}\n"

    prompt = f"""
    You are a Recruiter matching a candidate to jobs.
    Candidate Skills: {", ".join(skills_list)}
    Jobs to Evaluate:
    {job_text_block}
    Task: Rate each job (0-100) based on relevance to candidate skills.
    Provide a SHORT 1-sentence reason.
    RETURN JSON ONLY:
    {{
        "scores": [
            {{ "id": 0, "score": 95, "reason": "Perfect match for Cloud skills." }}
        ]
    }}
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

# --- ROUTES ---

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

    # 1. Main Title
    pdf.set_font("Arial", "B", 16)
    pdf.set_text_color(30, 58, 138)
    # CLEAN THIS
    role_title = clean_text(cv_data.get('role_title', 'CANDIDATE PROFILE'))
    pdf.cell(0, 10, role_title, 0, 1, 'L')
    
    # 2. Subtitle
    pdf.set_font("Arial", "I", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, "Anonymized Curriculum Vitae", 0, 1, 'L')
    pdf.ln(5)

    # 3. Summary
    if cv_data.get('summary'):
        pdf.section_header("Professional Summary")
        pdf.section_body(cv_data.get('summary'))

    # 4. Skills
    if cv_data.get('skills'):
        pdf.section_header("Skills")
        skills_str = ", ".join(cv_data.get('skills', []))
        pdf.section_body(skills_str)

    # 5. Experience
    if cv_data.get('experience'):
        pdf.section_header("Professional Experience")
        for job in cv_data['experience']:
            # Bold Job Title & Company (CLEANED)
            pdf.set_font("Arial", "B", 10)
            title = clean_text(job.get('title', ''))
            company = clean_text(job.get('company', ''))
            pdf.cell(0, 5, f"{title} | {company}", 0, 1)
            
            # Italic Dates (CLEANED)
            pdf.set_font("Arial", "I", 9)
            pdf.set_text_color(80, 80, 80)
            dates = clean_text(job.get('dates', ''))
            pdf.cell(0, 5, dates, 0, 1)
            
            # Normal Description
            pdf.set_text_color(0, 0, 0)
            pdf.section_body(job.get('description', ''))
            pdf.ln(2)

    # 6. Education
    if cv_data.get('education'):
        pdf.section_header("Education")
        for edu in cv_data['education']:
            # CLEAN ALL FIELDS
            school = clean_text(edu.get('school', ''))
            degree = clean_text(edu.get('degree', ''))
            dates = clean_text(edu.get('dates', ''))

            pdf.set_font("Arial", "B", 10)
            pdf.cell(0, 5, school, 0, 1)
            pdf.set_font("Arial", "", 10)
            pdf.cell(0, 5, f"{degree} | {dates}", 0, 1)
            pdf.ln(2)

    # 7. Languages
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
    candidate_name = data.get('real_name', 'Unknown') # Make sure we get the name
    candidate_skills = data.get('cv_skills', []) 
    
    # 1. SMART QUERIES
    search_queries = []
    for t in raw_titles[:2]: 
        clean_t = re.sub(r'\(.*?\)', '', t).replace('/', ' ').strip()
        if clean_t not in search_queries: search_queries.append(clean_t)
        words = clean_t.split()
        if len(words) >= 2:
            short_t = " ".join(words[:2])
            if short_t not in search_queries: search_queries.append(short_t)
    
    # 2. JOOBLE FETCH (UK)
    JOOBLE_KEY = os.environ.get("JOOBLE_KEY")
    API_URL = "https://jooble.org/api/" + JOOBLE_KEY
    
    raw_results = []
    seen_urls = set()

    for title in search_queries:
        if len(raw_results) >= 20: break 
        
        payload = { "keywords": title, "location": "United Kingdom", "page": 1 }

        try:
            response = requests.post(API_URL, json=payload)
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

    # 3. BLOCKLIST
    forbidden_words = [
        "recruitment", "recruiter", "talent acquisition", "hr manager", "human resources", 
        "headhunter", "agency", "staffing", "selection", "hiring",
        "supply chain", "logistics", "warehouse", "operations manager", 
        "store manager", "facility", "coordinator", "nurse", "teacher", 
        "driver", "mechanic", "cleaner", "internship", "apprentice", "trainee"
    ]
    
    final_jobs = []
    
    for job in raw_results:
        title = job.get('title', '').lower()
        company = job.get('company', '').lower()
        link = job.get('link')

        if any(bad in title for bad in forbidden_words): continue
        if any(bad in company for bad in forbidden_words): continue
        
        final_jobs.append({
            "title": job.get('title'),
            "company": job.get('company', 'Unknown'),
            "location": job.get('location', 'UK'),
            "job_url": link,
            "description": job.get('snippet', ''),
            "match_score": 0,
            "match_reason": "Analyzing..."
        })

    final_jobs = final_jobs[:10]

    # 4. AI SCORING
    if candidate_skills:
        final_jobs = get_ai_scores(final_jobs, candidate_skills)

    # --- 5. SAVE TO GOOGLE SHEETS (THIS WAS MISSING) ---
    if sheet and final_jobs:
        print("📝 Saving to Google Sheets...")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for job in final_jobs:
            try:
                # Format: [Timestamp, Candidate Name, Job Title, Company, Location, Link, Score]
                sheet.append_row([
                    timestamp, 
                    candidate_name, 
                    job['title'], 
                    job['company'], 
                    job['location'], 
                    job['job_url'],
                    job.get('match_score', 0)
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