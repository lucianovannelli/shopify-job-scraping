import os
import sys
import json
import hashlib
import time
from datetime import datetime
from typing import List, Optional, Dict, Any
import boto3
from botocore.exceptions import ClientError
from google import genai
from google.genai import types
from jobspy import scrape_jobs

# 1. JSON Schema for structured output
shopify_job_post_schema = {
    "type": "OBJECT",
    "properties": {
        "id": {"type": "STRING", "description": "Unique hash of the job URL"},
        "scraped_at": {"type": "STRING", "description": "ISO 8601 format timestamp of when the job was scraped"},
        "source": {"type": "STRING", "description": "The source site (e.g. linkedin, indeed, zip_recruiter, glassdoor)"},
        "url": {"type": "STRING", "description": "URL of the job posting"},
        "title": {"type": "STRING", "description": "Job title"},
        "company": {"type": "STRING", "description": "Company name"},
        "location": {"type": "STRING", "description": "Location of the job (city, state/country)"},
        "remote": {"type": "BOOLEAN", "description": "True if remote, False otherwise"},
        "shopify_related": {
            "type": "BOOLEAN", 
            "description": "True if the job is specifically for the Shopify platform/ecosystem (e.g., developing themes, apps, managing Shopify stores, Shopify marketing). False if Shopify is just mentioned as a client name or minor integration, or is completely unrelated."
        },
        "seniority": {
            "type": "STRING",
            "description": "Seniority level. Must be exactly one of: junior, mid, senior, lead, not_specified",
            "enum": ["junior", "mid", "senior", "lead", "not_specified"]
        },
        "role_type": {
            "type": "STRING",
            "description": "Role type. Must be exactly one of: developer, designer, pm, marketer, analyst, other",
            "enum": ["developer", "designer", "pm", "marketer", "analyst", "other"]
        },
        "shopify_focus": {
            "type": "STRING",
            "description": "Primary Shopify focus. Must be exactly one of: theme, app_dev, plus, headless, general",
            "enum": ["theme", "app_dev", "plus", "headless", "general"]
        },
        "technologies": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "List of technologies mentioned (e.g. Liquid, React, Hydrogen, Tailwind, Node.js, Ruby, Remix)"
        },
        "soft_skills": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "List of soft skills mentioned (e.g. communication, project management, problem solving)"
        },
        "salary_min": {"type": "NUMBER", "description": "Minimum salary in the posting, if listed. Otherwise null."},
        "salary_max": {"type": "NUMBER", "description": "Maximum salary in the posting, if listed. Otherwise null."},
        "salary_currency": {"type": "STRING", "description": "ISO currency code for the salary (e.g. USD, EUR, GBP). Otherwise null."},
        "raw_description": {"type": "STRING", "description": "Full or summarized description text of the job posting"}
    },
    "required": [
        "id", "scraped_at", "source", "url", "title", "company", "location", "remote",
        "shopify_related", "seniority", "role_type", "shopify_focus", "technologies", "soft_skills", "raw_description"
    ]
}

def get_hash(val: str) -> str:
    return hashlib.md5(val.encode('utf-8')).hexdigest()

def clean_value(val: Any) -> str:
    if val is None or (isinstance(val, float) and sys.float_info.max == val) or str(val) == "nan":
        return ""
    return str(val).strip()

def main():
    print("Starting Shopify job scraping process...")
    
    # Check Gemini API Key
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        print("ERROR: GEMINI_API_KEY environment variable is not set.")
        sys.exit(1)
        
    client = genai.Client(api_key=gemini_key)
    
    # R2 configuration check
    r2_endpoint = os.environ.get("R2_ENDPOINT_URL")
    r2_key_id = os.environ.get("R2_ACCESS_KEY_ID")
    r2_secret = os.environ.get("R2_SECRET_ACCESS_KEY")
    r2_bucket = os.environ.get("R2_BUCKET_NAME")
    
    r2_configured = all([r2_endpoint, r2_key_id, r2_secret, r2_bucket])
    if not r2_configured:
        print("WARNING: R2 environment variables are not fully set. Output will only be saved locally.")
    
    # 2. Run JobSpy Scraping
    queries = ["shopify developer", "shopify plus", "liquid developer", "shopify app developer"]
    scraped_raw_jobs = []
    
    print("Scraping jobs using python-jobspy...")
    for query in queries:
        print(f"Searching for: '{query}'...")
        try:
            # We look for postings in the last 7 days (168 hours)
            jobs = scrape_jobs(
                site_name=["linkedin", "indeed", "zip_recruiter", "glassdoor"],
                search_term=query,
                results_wanted=15,
                hours_old=168,
            )
            
            if not jobs.empty:
                print(f"Found {len(jobs)} postings for '{query}'")
                for _, row in jobs.iterrows():
                    job_url = clean_value(row.get("job_url"))
                    if not job_url:
                        continue
                        
                    # Basic mapping from JobSpy dataframe columns
                    job_data = {
                        "url": job_url,
                        "title": clean_value(row.get("title")),
                        "company": clean_value(row.get("company")),
                        "location": clean_value(row.get("location")),
                        "description": clean_value(row.get("description")),
                        "site": clean_value(row.get("site")),
                    }
                    scraped_raw_jobs.append(job_data)
            else:
                print(f"No postings found for '{query}'")
        except Exception as e:
            print(f"Error scraping '{query}': {e}")
            
    # Deduplicate raw jobs by Title + Company (normalized) to avoid duplicates
    unique_raw_jobs = {}
    for job in scraped_raw_jobs:
        title_norm = "".join(e for e in job["title"].lower() if e.isalnum())
        company_norm = "".join(e for e in job["company"].lower() if e.isalnum())
        dedup_key = f"{title_norm}_{company_norm}"
        
        if dedup_key not in unique_raw_jobs:
            unique_raw_jobs[dedup_key] = job
        elif not unique_raw_jobs[dedup_key]["description"] and job["description"]:
            unique_raw_jobs[dedup_key] = job
            
    print(f"Total unique raw job postings scraped: {len(unique_raw_jobs)}")
    
    # 3. Download existing data.json from R2 to merge and avoid deleting past history
    existing_jobs: Dict[str, Dict[str, Any]] = {}
    s3_client = None
    
    if r2_configured:
        try:
            s3_client = boto3.client(
                's3',
                endpoint_url=r2_endpoint,
                aws_access_key_id=r2_key_id,
                aws_secret_access_key=r2_secret
            )
            print(f"Checking for existing data.json in R2 bucket '{r2_bucket}'...")
            response = s3_client.get_object(Bucket=r2_bucket, Key='data.json')
            existing_data = json.loads(response['Body'].read().decode('utf-8'))
            if isinstance(existing_data, list):
                for job in existing_data:
                    existing_jobs[job["id"]] = job
            elif isinstance(existing_data, dict) and "jobs" in existing_data:
                # support both formats
                for job in existing_data["jobs"]:
                    existing_jobs[job["id"]] = job
            print(f"Loaded {len(existing_jobs)} existing job records from R2.")
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                print("No existing data.json found in R2 bucket. Starting fresh.")
            else:
                print(f"Error connecting to R2 / reading existing data: {e}")
        except Exception as e:
            print(f"Unexpected error loading existing data: {e}")
            
    # 4. Extract structured data with Gemini API
    # Build a set of existing title+company keys to check against to avoid duplicates
    existing_keys = set()
    for job_record in existing_jobs.values():
        t_norm = "".join(e for e in job_record.get("title", "").lower() if e.isalnum())
        c_norm = "".join(e for e in job_record.get("company", "").lower() if e.isalnum())
        existing_keys.add(f"{t_norm}_{c_norm}")

    processed_count = 0
    scraped_at_timestamp = datetime.utcnow().isoformat() + "Z"
    
    for dedup_key, job in unique_raw_jobs.items():
        url_hash = get_hash(job["url"])
        
        # Skip if we already have it in the database by URL hash or by title/company
        if url_hash in existing_jobs or dedup_key in existing_keys:
            continue
            
        print(f"Processing new job: {job['title']} at {job['company']}...")
        
        prompt = f"""
        Extract structured details from the following job posting information:
        
        Job Title: {job['title']}
        Company: {job['company']}
        Location: {job['location']}
        Source: {job['site']}
        URL: {job['url']}
        Description:
        {job['description'][:8000]} # Truncate to avoid huge inputs
        
        Ensure that seniority is strictly one of: junior, mid, senior, lead, not_specified.
        Ensure that role_type is strictly one of: developer, designer, pm, marketer, analyst, other.
        Ensure that shopify_focus is strictly one of: theme, app_dev, plus, headless, general.
        """
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Add delay to avoid rate limiting (5 seconds keeps us under 12 RPM)
                time.sleep(5)
                
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=shopify_job_post_schema
                    )
                )
                
                structured_data = json.loads(response.text)
                
                # Check if Gemini classified this job as Shopify-related
                if not structured_data.get("shopify_related", True):
                    print(f"Skipping job (not Shopify related): {job['title']} at {job['company']}")
                    break  # Discard and break retry loop
                
                # Fill mandatory/system fields
                structured_data["id"] = url_hash
                structured_data["scraped_at"] = scraped_at_timestamp
                structured_data["url"] = job["url"]
                if not structured_data.get("source"):
                    structured_data["source"] = job["site"] or "unknown"
                    
                existing_jobs[url_hash] = structured_data
                processed_count += 1
                print(f"Successfully processed job: {structured_data['title']} ({structured_data['shopify_focus']}/{structured_data['role_type']})")
                break  # Exit retry loop on success
                
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    print(f"Rate limit hit on attempt {attempt+1}/{max_retries}. Sleeping 60s before retry...")
                    time.sleep(60)
                else:
                    print(f"Error processing job with Gemini API: {e}")
                    break  # Exit retry loop on other errors
            
    print(f"Done processing. Added {processed_count} new jobs.")
    
    # 5. Save and Upload
    # Convert back to list format
    all_jobs_list = list(existing_jobs.values())
    
    # Sort by scraped_at descending so newest are first
    all_jobs_list.sort(key=lambda x: x.get("scraped_at", ""), reverse=True)
    
    # Save locally first
    local_output = "data.json"
    with open(local_output, "w", encoding="utf-8") as f:
        json.dump(all_jobs_list, f, indent=2, ensure_ascii=False)
    print(f"Successfully saved {len(all_jobs_list)} total jobs to {local_output}")
    
    # Upload to R2
    if r2_configured and s3_client:
        try:
            print(f"Uploading data.json to Cloudflare R2 bucket '{r2_bucket}'...")
            s3_client.upload_file(
                Filename=local_output,
                Bucket=r2_bucket,
                Key='data.json',
                ExtraArgs={
                    'ContentType': 'application/json',
                    'CacheControl': 'max-age=3600' # cache for 1 hour
                }
            )
            print("Upload to R2 completed successfully!")
        except Exception as e:
            print(f"Error uploading to R2: {e}")
            
if __name__ == "__main__":
    main()
