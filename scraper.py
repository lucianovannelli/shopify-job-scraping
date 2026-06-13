import os
import sys
import json
import hashlib
import time
from datetime import datetime
from typing import List, Optional, Dict, Any
import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field
import google.generativeai as genai
from jobspy import scrape_jobs

# 1. Pydantic schema for structured output
class ShopifyJobPost(BaseModel):
    id: str = Field(description="Unique hash of the job URL")
    scraped_at: str = Field(description="ISO 8601 format timestamp of when the job was scraped")
    source: str = Field(description="The source site (e.g. linkedin, indeed, zip_recruiter, glassdoor)")
    url: str = Field(description="URL of the job posting")
    title: str = Field(description="Job title")
    company: str = Field(description="Company name")
    location: str = Field(description="Location of the job (city, state/country)")
    remote: bool = Field(description="True if remote, False otherwise")
    seniority: str = Field(description="Seniority level. Must be exactly one of: junior, mid, senior, lead, not_specified")
    role_type: str = Field(description="Role type. Must be exactly one of: developer, designer, pm, marketer, analyst, other")
    shopify_focus: str = Field(description="Primary Shopify focus. Must be exactly one of: theme, app_dev, plus, headless, general")
    technologies: List[str] = Field(description="List of technologies mentioned (e.g. Liquid, React, Hydrogen, Tailwind, Node.js, Ruby, Remix)")
    soft_skills: List[str] = Field(description="List of soft skills mentioned (e.g. communication, project management, problem solving)")
    salary_min: Optional[float] = Field(None, description="Minimum salary in the posting, if listed. Otherwise null.")
    salary_max: Optional[float] = Field(None, description="Maximum salary in the posting, if listed. Otherwise null.")
    salary_currency: Optional[str] = Field(None, description="ISO currency code for the salary (e.g. USD, EUR, GBP). Otherwise null.")
    raw_description: str = Field(description="Full or summarized description text of the job posting")

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
        
    genai.configure(api_key=gemini_key)
    
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
            
    # Deduplicate raw jobs by URL hash
    unique_raw_jobs = {}
    for job in scraped_raw_jobs:
        job_hash = get_hash(job["url"])
        if job_hash not in unique_raw_jobs:
            unique_raw_jobs[job_hash] = job
            
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
    model = genai.GenerativeModel('gemini-1.5-flash')
    processed_count = 0
    
    scraped_at_timestamp = datetime.utcnow().isoformat() + "Z"
    
    for job_id, job in unique_raw_jobs.items():
        # Skip if we already have it in the database (we don't need to re-extract)
        if job_id in existing_jobs:
            # We can update the scraped_at if we want, or just preserve the original posting
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
        
        try:
            # Add delay to avoid rate limiting
            time.sleep(2)
            
            response = model.generate_content(
                prompt,
                generation_config=genai.GenerationConfig(
                    response_mime_type="application/json",
                    response_schema=ShopifyJobPost
                )
            )
            
            structured_data = json.loads(response.text)
            
            # Fill mandatory/system fields
            structured_data["id"] = job_id
            structured_data["scraped_at"] = scraped_at_timestamp
            structured_data["url"] = job["url"]
            if not structured_data["source"]:
                structured_data["source"] = job["site"] or "unknown"
                
            existing_jobs[job_id] = structured_data
            processed_count += 1
            print(f"Successfully processed job: {structured_data['title']} ({structured_data['shopify_focus']}/{structured_data['role_type']})")
            
        except Exception as e:
            print(f"Error processing job with Gemini API: {e}")
            
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
