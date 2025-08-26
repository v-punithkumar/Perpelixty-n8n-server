import asyncio
import nest_asyncio
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import os
import subprocess
import socket
import logging
import time
import sys
import re
import requests
from pathlib import Path
from typing import Optional
import base64
import io
import json
from datetime import datetime
from playwright.async_api import async_playwright, Error as PlaywrightError

# Enable nested event loops for Flask async compatibility
nest_asyncio.apply()

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Constants for Playwright Perplexity integration
S3_RE = re.compile(r'https://user-gen-media-assets\.s3\.amazonaws\.com/[^")\s]+')
IMAGEDL_RE = re.compile(r'https://imagedelivery\.net/[^")\s]+')
PERPLEXITY_URL = "https://www.perplexity.ai/search/a-split-image-on-one-side-glea-hNLvcaq8QUuuS6w.r.n9qA"  # Default URL that will always be used
TIMEOUT = int(os.environ.get("PERPLEXITY_WAIT_MS", "60000"))  # ms

async def find_image_url_from_page(page) -> Optional[str]:
    """Find image URL from page content"""
    # Try to find final s3 image first
    content = await page.content()
    m = S3_RE.search(content)
    if m:
        url = m.group(0)
        # Validate the URL format
        if url.endswith('.png') or url.endswith('.jpg') or url.endswith('.jpeg'):
            return url
    
    m2 = IMAGEDL_RE.search(content)
    if m2:
        url = m2.group(0)
        if url.endswith('.png') or url.endswith('.jpg') or url.endswith('.jpeg'):
            return url
    
    # try to find img[src] with proper validation
    found = None
    imgs = await page.query_selector_all("img")
    for img in imgs:
        src = await img.get_attribute("src")
        if src:
            if "user-gen-media-assets.s3.amazonaws.com" in src and (src.endswith('.png') or src.endswith('.jpg') or src.endswith('.jpeg')):
                return src
            if "imagedelivery.net" in src and (src.endswith('.png') or src.endswith('.jpg') or src.endswith('.jpeg')):
                found = src
    return found

def launch_brave_windows(user_data_dir: str = r"C:\temp\brave_debug_profile") -> None:
    """Launch Brave on Windows with remote debugging port 9222"""
    brave_path = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
    # Build the PowerShell command exactly as requested.
    ps_cmd = (
        "$brave = '{brave}'; Start-Process -FilePath $brave -ArgumentList '--remote-debugging-port=9222','--user-data-dir={ud}'"
    ).format(brave=brave_path.replace("'", "'"), ud=user_data_dir.replace("'", "'"))
    try:
        # Use powershell -NoProfile -Command to start Brave detached.
        subprocess.Popen([
            "powershell",
            "-NoProfile",
            "-Command",
            ps_cmd,
        ])
        logger.info(f"Launched Brave with user-data-dir={user_data_dir}")
    except Exception as e:
        logger.error(f"Failed to launch Brave via PowerShell: {e}")

def wait_for_cdp(cdp_url: str, timeout_s: int = 10) -> bool:
    """Poll the CDP endpoint /json/version or /json until it responds."""
    deadline = time.time() + timeout_s
    url = cdp_url.rstrip("/") + "/json/version"
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=1)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False

def ensure_debug_dir() -> Path:
    p = Path("./debug_screens")
    p.mkdir(parents=True, exist_ok=True)
    return p

def download_and_encode_image(image_url: str) -> Optional[dict]:
    """Download image from URL and return as base64 encoded data with metadata"""
    try:
        logger.info(f"Downloading image from: {image_url}")
        
        # Download the image
        response = requests.get(image_url, timeout=30)
        response.raise_for_status()
        
        # Get image data
        image_data = response.content
        
        # Determine content type
        content_type = response.headers.get('content-type', 'image/png')
        if not content_type.startswith('image/'):
            # Try to determine from URL extension
            if image_url.endswith('.jpg') or image_url.endswith('.jpeg'):
                content_type = 'image/jpeg'
            elif image_url.endswith('.png'):
                content_type = 'image/png'
            else:
                content_type = 'image/png'  # default
        
        # Encode to base64
        base64_data = base64.b64encode(image_data).decode('utf-8')
        
        # Create data URI
        data_uri = f"data:{content_type};base64,{base64_data}"
        
        logger.info(f"Successfully downloaded and encoded image ({len(image_data)} bytes)")
        
        return {
            "base64": base64_data,
            "dataUri": data_uri,
            "contentType": content_type,
            "size": len(image_data),
            "originalUrl": image_url
        }
        
    except Exception as e:
        logger.error(f"Failed to download and encode image from {image_url}: {e}")
        return None

async def generate_image_with_playwright(prompt_text: str) -> Optional[str]:
    """Generate image using Playwright automation on Perplexity"""
    # Get CDP URL from environment
    debug_port = int(os.getenv('BRAVE_DEBUG_PORT', '9222'))
    cdp_url = f"http://127.0.0.1:{debug_port}"
    
    # If the CDP endpoint isn't reachable, try launching Brave on Windows
    if sys.platform.startswith("win"):
        try:
            resp = requests.get(cdp_url.rstrip("/") + "/json", timeout=0.8)
            ok = resp.status_code == 200
        except Exception:
            ok = False
        if not ok:
            logger.info("CDP not reachable, launching Brave on Windows...")
            launch_brave_windows()
            # wait briefly for CDP
            if not wait_for_cdp(cdp_url, timeout_s=20):
                logger.error(f"Timed out waiting for CDP at {cdp_url}")
                return None
    
    async with async_playwright() as pw:
        # Connect to existing browser via ws endpoint from DevTools
        try:
            logger.info(f"Connecting to CDP endpoint: {cdp_url}")
            browser = await pw.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            logger.error(f"Failed to connect to CDP at {cdp_url}: {e}")
            return None

        try:
            # Find an existing Perplexity page in any context
            found_page = None
            try:
                for ctx in browser.contexts:
                    for p in ctx.pages:
                        try:
                            url = p.url
                        except Exception:
                            url = None
                        if url and (PERPLEXITY_URL in url or 'perplexity.ai/search' in url or 'perplexity.ai' in url):
                            found_page = p
                            break
                    if found_page:
                        break
            except Exception as e:
                logger.error(f"Error while scanning existing pages: {e}")

            if found_page:
                page = found_page
                logger.info(f"Re-using existing Perplexity page: {page.url}")
            else:
                # Only open new page if no Perplexity page exists
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(PERPLEXITY_URL, wait_until="domcontentloaded")
                logger.info(f"Opened new Perplexity page: {PERPLEXITY_URL}")
            
            # Allow content to load
            await page.wait_for_timeout(1000)

            debug_dir = ensure_debug_dir()
            
            # No unnecessary navigation - work with the current page
            logger.info(f"Working with current page: {page.url}")
            
            # Scroll to bottom to see the input area
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(500)
            
            # Store existing images before submitting prompt so we can detect new ones
            existing_images = set()
            try:
                existing_img_elements = await page.query_selector_all('img[src*="user-gen-media-assets.s3.amazonaws.com"], img[src*="imagedelivery.net"]')
                for img_el in existing_img_elements:
                    src = await img_el.get_attribute('src')
                    if src:
                        existing_images.add(src)
                logger.info(f"Found {len(existing_images)} existing images before submission")
            except Exception:
                pass
            
            # Look for the input field - focus on the specific contenteditable div
            input_found = False
            input_selectors = [
                '#ask-input',  # Main input ID - this should be the primary target
                'div[contenteditable="true"][aria-placeholder*="follow-up"]',  # Follow-up input
                'div[contenteditable="true"][role="textbox"]',  # Textbox role
                'div[contenteditable="true"]'  # Any contenteditable as fallback
            ]
            
            for selector in input_selectors:
                try:
                    element = await page.query_selector(selector)
                    if element:
                        logger.info(f"Found input using selector: {selector}")
                        
                        try:
                            # Check if element is visible and enabled
                            is_visible = await element.is_visible()
                            if not is_visible:
                                logger.debug(f"Input element {selector} not visible, skipping")
                                continue
                            
                            # Clear any existing content and focus
                            await element.click()
                            await page.wait_for_timeout(500)
                            
                            # Clear content by selecting all and deleting
                            await page.keyboard.press('Control+a')
                            await page.wait_for_timeout(200)
                            await page.keyboard.press('Delete')
                            await page.wait_for_timeout(300)
                            
                            # Insert the new prompt via JS to avoid long typing timeouts on contenteditable fields
                            try:
                                await page.evaluate("(el, value) => { el.focus(); if (el.isContentEditable) { el.innerText = value; } else { el.value = value; } el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }", element, prompt_text)
                                await page.wait_for_timeout(800)
                            except Exception as js_err:
                                logger.debug(f"JS paste failed, falling back to typing: {js_err}")
                                # Fallback to typing if JS paste fails
                                delay_ms = max(5, min(20, 1000 // max(1, len(prompt_text))))
                                await element.type(prompt_text, delay=delay_ms, timeout=120000)
                                await page.wait_for_timeout(800)
                            
                            # Wait for any UI updates after typing
                            await page.wait_for_timeout(500)
                            
                            # Try multiple submission methods
                            submitted = False
                            
                            # Method 1: Look for enabled submit button first
                            submit_selectors = [
                                '[data-testid="submit-button"]:not([disabled])',
                                'button[type="submit"]:not([disabled])',
                                'button:has-text("Send"):not([disabled])',
                                'button[aria-label*="Send"]:not([disabled])'
                            ]
                            
                            for submit_sel in submit_selectors:
                                submit_btn = await page.query_selector(submit_sel)
                                if submit_btn:
                                    try:
                                        is_enabled = await submit_btn.is_enabled()
                                        is_visible = await submit_btn.is_visible()
                                        if is_enabled and is_visible:
                                            logger.info(f"Clicking submit button: {submit_sel}")
                                            await submit_btn.click()
                                            submitted = True
                                            break
                                    except Exception:
                                        continue
                            
                            # Method 2: If no submit button worked, try Enter key
                            if not submitted:
                                logger.info("No enabled submit button found, trying Enter key")
                                await element.focus()
                                await page.wait_for_timeout(200)
                                await page.keyboard.press('Enter')
                                submitted = True
                            
                            if submitted:
                                logger.info("Prompt submitted successfully")
                                # Wait a bit to ensure submission is processed
                                await page.wait_for_timeout(2000)
                                input_found = True
                                break
                            
                        except Exception as input_error:
                            logger.error(f"Error interacting with input element {selector}: {input_error}")
                            continue
                            
                except Exception as e:
                    logger.debug(f"Error with selector {selector}: {e}")
                    continue
            
            if not input_found:
                logger.error("Could not find input field to submit prompt")
                return None
            
            logger.info("Prompt submitted successfully")
            
            # Wait for generation to start
            await page.wait_for_timeout(2000)
            
            # Now wait for the new image to be generated and capture it
            start = time.time()
            last_save = 0
            img = None
            generation_started = False
            
            try:
                while (time.time() - start) * 1000 < TIMEOUT:
                    # Look for the grid overlay pattern which indicates generation is happening
                    grid_overlay = await page.query_selector('.pointer-events-none.absolute.inset-0.z-\\[5\\].grid')
                    if not grid_overlay:
                        # Try alternative selector for the grid pattern
                        grid_overlay = await page.query_selector('div[style*="grid-template-columns: repeat(auto-fill"]')
                    
                    # Check if we have grid elements with opacity styles (generation in progress)
                    has_grid_pattern = False
                    if grid_overlay:
                        grid_children = await grid_overlay.query_selector_all('div[style*="opacity:"]')
                        if len(grid_children) > 0:
                            has_grid_pattern = True
                    
                    # Look for the actual image element with src attribute
                    img_elements = await page.query_selector_all('img[src*="user-gen-media-assets.s3.amazonaws.com"], img[src*="imagedelivery.net"]')
                    current_img = None
                    
                    for img_el in img_elements:
                        try:
                            src = await img_el.get_attribute('src')
                            if src and src not in existing_images:  # Only consider NEW images
                                # Validate the URL format
                                if (src.endswith('.png') or src.endswith('.jpg') or src.endswith('.jpeg')):
                                    logger.info(f"Found valid NEW image URL: {src}")
                                    current_img = src
                                    break
                                else:
                                    logger.debug(f"Found invalid image URL (not proper format): {src}")
                        except Exception:
                            continue
                    
                    # If we found an image and there's no grid pattern (generation complete)
                    if current_img and not has_grid_pattern:
                        logger.info(f"Found NEW image: {current_img}")
                        img = current_img
                        break
                    
                    # Check for other generation indicators
                    generating_elements = await page.query_selector_all('.animate-gradient, [class*="generating"], [class*="loading"]')
                    if generating_elements and not generation_started:
                        logger.info("Generation started (detected loading indicators)...")
                        generation_started = True
                    
                    # Save progress screenshot every few seconds (for debugging)
                    if time.time() - last_save > 3:
                        timestamp = int(time.time() * 1000) % 10000000
                        try:
                            status = "generating" if has_grid_pattern or generating_elements else "waiting"
                            await page.screenshot(path=str(debug_dir / f"progress_{timestamp}.png"), full_page=True)
                            logger.debug(f"Saved progress screenshot to debug_screens/progress_{timestamp}.png (status: {status})")
                            last_save = time.time()
                        except Exception:
                            pass
                    
                    await page.wait_for_timeout(1000)
                
                # Final check: if we have generation indicators but no image yet, wait a bit more
                if generation_started and not img:
                    logger.info("Generation was detected but no new image found yet, waiting additional time...")
                    extra_wait_start = time.time()
                    while (time.time() - extra_wait_start) < 15:
                        # Look for any new images that weren't there before
                        img_elements = await page.query_selector_all('img[src*="user-gen-media-assets.s3.amazonaws.com"], img[src*="imagedelivery.net"]')
                        for img_el in img_elements:
                            try:
                                src = await img_el.get_attribute('src')
                                if src and src not in existing_images:
                                    # Validate the URL format
                                    if (src.endswith('.png') or src.endswith('.jpg') or src.endswith('.jpeg')):
                                        logger.info(f"Found NEW image during extended wait: {src}")
                                        img = src
                                        break
                                    else:
                                        logger.debug(f"Found invalid image URL during extended wait: {src}")
                            except Exception:
                                continue
                        if img:
                            break
                        await page.wait_for_timeout(1000)
                
            except Exception as e:
                # Handle target closed errors gracefully
                msg = str(e)
                if "TargetClosedError" in msg or "has been closed" in msg:
                    logger.error(f"Detected browser/page closed: {e}")
                    return None
                raise
            
            if img:
                logger.info(f"Generation complete! Found final S3 image: {img}")
                return img
            else:
                logger.warning("No new image found after waiting")
                return None
                
        finally:
            try:
                await browser.close()
            except Exception:
                pass

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({"success": True, "message": "Service is healthy", "service": "perplexity-image-generator"})

@app.route('/split-linkedin', methods=['POST'])
def split_linkedin():
    """
    Split LinkedIn post into title and message.
    
    Expected JSON body:
    {
        "text": {
            "LinkedInPost": "Your LinkedIn post content here",
            "ImagePrompt": "Optional image prompt"
        }
    }
    OR
    {
        "LinkedInPost": "Your LinkedIn post content here",
        "ImagePrompt": "Optional image prompt"
    }
    OR
    {
        "Text": "JSON string containing LinkedInPost and ImagePrompt"
    }
    
    Returns:
    {
        "success": true,
        "data": {
            "title": "Extracted title with emoji",
            "message": "Rest of the message",
            "hashtags": ["extracted", "hashtags"],
            "imagePrompt": "Original image prompt if provided"
        }
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({
                "success": False,
                "message": "No JSON data provided",
                "error": "MISSING_DATA"
            }), 400
        
        # Handle all possible formats
        linkedin_post = None
        image_prompt = None
        
        # Handle case where Text field contains JSON string
        if 'Text' in data:
            try:
                text_json = json.loads(data['Text'])
                linkedin_post = text_json.get('LinkedInPost')
                image_prompt = text_json.get('ImagePrompt')
            except json.JSONDecodeError:
                return jsonify({
                    "success": False,
                    "message": "Invalid JSON in Text field",
                    "error": "INVALID_JSON"
                }), 400
        # Handle case where text field is a dict
        elif 'text' in data and isinstance(data['text'], dict):
            linkedin_post = data['text'].get('LinkedInPost')
            image_prompt = data['text'].get('ImagePrompt')
        # Handle direct fields
        elif 'LinkedInPost' in data:
            linkedin_post = data['LinkedInPost']
            image_prompt = data.get('ImagePrompt')
        
        if not linkedin_post:
            return jsonify({
                "success": False,
                "message": "No LinkedInPost found in request",
                "error": "MISSING_LINKEDIN_POST"
            }), 400
            
        # Clean up the post text
        post_text = linkedin_post.strip()
        
        # Extract title and message
        post_text = linkedin_post.strip()
        
        # Extract title and message
        title = ""
        message = post_text
        
        # Pattern 1: Question ending with ? and thinking emoji 🤔
        title_match = re.match(r'^([^?]*\? 🤔)', post_text)
        if title_match:
            title = title_match.group(1).strip()
            # Keep the title in the message for better context
            message = post_text.strip()
        else:
            # Pattern 2: First sentence ending with punctuation
            title_match = re.match(r'^([^.!?\n]*[.!?])', post_text)
            if title_match:
                title = title_match.group(1).strip()
                message = post_text.strip()  # Keep full text as message
            else:
                # Pattern 3: First line break
                lines = post_text.split('\n', 1)
                if len(lines) > 1:
                    title = lines[0].strip()
                    message = post_text.strip()  # Keep full text as message
                else:
                    # Fallback: first sentence as title
                    title = post_text[:100].strip() + "..."
                    message = post_text.strip()
        hashtags = re.findall(r'#\w+', post_text)
        
        # Remove leading whitespace and newlines from message
        message = message.lstrip()
        
        # Create response
        response_data = {
            "success": True,
            "data": {
                "title": title,
                "message": message,
                "hashtags": hashtags,
                "statistics": {
                    "titleLength": len(title),
                    "messageLength": len(message),
                    "hashtagCount": len(hashtags)
                }
            }
        }
        
        # Add image prompt if it exists
        if image_prompt:
            response_data["data"]["imagePrompt"] = image_prompt
        
        return jsonify(response_data)
        
    except Exception as e:
        logger.error(f"Error processing LinkedIn post: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Error processing request: {str(e)}",
            "error": "PROCESSING_ERROR"
        }), 500

@app.route('/image-only', methods=['POST'])
def image_only():
    """
    Generate an image and return it in binary format compatible with n8n.
    
    Expected JSON body:
    {
        "prompt": "A description of the image you want to generate"
    }
    OR
    {
        "Text": "JSON string containing LinkedInPost and ImagePrompt"
    }
    
    Returns binary image data with proper headers for n8n compatibility.
    """
    try:
        data = request.get_json()
        
        # Try to get prompt directly
        if data and 'prompt' in data:
            prompt = data['prompt']
        # Try to parse Text field containing JSON
        elif data and 'Text' in data:
            try:
                json_content = json.loads(data['Text'])
                if 'ImagePrompt' in json_content:
                    prompt = json_content['ImagePrompt']
                elif 'LinkedInPost' in json_content:
                    # Create an image prompt from LinkedIn post
                    linkedin_text = json_content['LinkedInPost']
                    if 'AI' in linkedin_text:
                        prompt = "Create a professional image showing the impact of AI on society, highlighting both benefits and challenges"
                    else:
                        prompt = "Create a professional business-themed image"
                else:
                    return jsonify({
                        "success": False,
                        "message": "No ImagePrompt or LinkedInPost found in Text JSON",
                        "error": "MISSING_PARAMETER"
                    }), 400
            except json.JSONDecodeError:
                return jsonify({
                    "success": False,
                    "message": "Invalid JSON in Text field",
                    "error": "INVALID_JSON"
                }), 400
        else:
            return jsonify({
                "success": False,
                "message": "Missing required 'prompt' or 'Text' field in request body",
                "error": "MISSING_PARAMETER"
            }), 400
        
        # Generate the image using playwright
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            image_url = loop.run_until_complete(generate_image_with_playwright(prompt))
            
            if image_url:
                # Download and get the raw image data
                response = requests.get(image_url, timeout=30)
                response.raise_for_status()
                
                # Get content type and filename
                content_type = response.headers.get('content-type', 'image/png')
                if not content_type.startswith('image/'):
                    if image_url.endswith('.jpg') or image_url.endswith('.jpeg'):
                        content_type = 'image/jpeg'
                    else:
                        content_type = 'image/png'
                
                # Create filename with timestamp
                extension = 'jpg' if 'jpeg' in content_type else 'png'
                filename = f"generated_image_{int(time.time())}.{extension}"
                
                # Return the binary data with proper headers for n8n
                headers = {
                    'Content-Type': content_type,
                    'Content-Disposition': f'attachment; filename="{filename}"'
                }
                return response.content, 200, headers
                
            else:
                return jsonify({
                    "success": False,
                    "message": "Failed to generate image",
                    "error": "GENERATION_FAILED"
                }), 500
                
        finally:
            loop.close()
            
    except Exception as e:
        logger.error(f"Error in image generation: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Server error: {str(e)}",
            "error": "INTERNAL_ERROR"
        }), 500

@app.route('/generate-image-raw', methods=['POST'])
def generate_image_raw():
    """
    Generate an image using raw text input - handles n8n template issues.
    
    This endpoint accepts any raw text content and tries to extract prompts from it.
    Specifically designed to handle n8n template expressions that can't be parsed as valid JSON.
    
    Returns:
    {
        "success": true,
        "data": {
            "imageUrl": "https://user-gen-media-assets.s3.amazonaws.com/...",
            "imageData": {...}
        }
    }
    """
    try:
        # Get raw content regardless of content type
        raw_content = request.get_data(as_text=True)
        logger.info(f"Raw endpoint received content (first 300 chars): {raw_content[:300]}...")
        
        if not raw_content:
            return jsonify({
                "success": False, 
                "message": "No content received in request body", 
                "error": "EMPTY_BODY"
            }), 400
        
        # Extract prompt from various patterns
        post_text = None
        
        # Pattern 1: Look for "prompt": "..." in the raw content
        import re
        prompt_patterns = [
            r'"prompt":\s*"([^"]*(?:\\.[^"]*)*)"',  # Standard JSON prompt field
            r'ImagePrompt["\']?\s*:\s*["\']([^"\']*)["\']',  # ImagePrompt field
            r'postText["\']?\s*:\s*["\']([^"\']*)["\']',  # postText field
        ]
        
        for pattern in prompt_patterns:
            match = re.search(pattern, raw_content, re.DOTALL | re.IGNORECASE)
            if match:
                post_text = match.group(1).replace('\\"', '"').replace('\\n', '\n')
                logger.info(f"Extracted prompt using pattern {pattern}: {post_text[:100]}...")
                break
        
        # If no specific prompt found, use the entire content
        if not post_text:
            post_text = raw_content
            logger.info(f"Using entire raw content as prompt: {post_text[:100]}...")
        
        # Apply the same JSON parsing logic for extracting ImagePrompt
        if post_text and len(post_text) > 50:  # Only for longer content
            # Look for embedded JSON with ImagePrompt
            json_pattern = r'```json\s*\n?(.*?)\n?```'
            json_match = re.search(json_pattern, post_text, re.DOTALL)
            
            if json_match:
                try:
                    json_content = json_match.group(1).strip()
                    parsed_json = json.loads(json_content)
                    
                    if 'ImagePrompt' in parsed_json:
                        image_prompt = parsed_json['ImagePrompt']
                        logger.info(f"Using ImagePrompt from raw parsed JSON: {image_prompt}")
                        post_text = image_prompt
                    
                except Exception as e:
                    logger.warning(f"Failed to parse embedded JSON from raw content: {e}")
        
        # Do not forcibly truncate prompts. Allow full ImagePrompt or provided text
        # but cap at a high safety limit to avoid pathological sizes.
        MAX_PROMPT_LENGTH = 8000
        if len(post_text) > MAX_PROMPT_LENGTH:
            post_text = post_text[:MAX_PROMPT_LENGTH]
            logger.info(f"Truncated prompt to {MAX_PROMPT_LENGTH} characters for safety")
        
        logger.info(f"Final prompt to be used: {post_text}")
        
        # Generate the image using the existing async function
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            image_url = loop.run_until_complete(generate_image_with_playwright(post_text))
        except Exception as e:
            logger.error(f"Error generating image: {str(e)}")
            image_url = None
        
        if image_url:
            # Download the image and encode as base64
            image_data = download_and_encode_image(image_url)
            
            response_data = {
                "success": True,
                "data": {
                    "imageUrl": image_url,
                    "prompt": post_text,
                    "timestamp": datetime.now().isoformat(),
                    "imageData": image_data
                }
            }
            
            # Extract LinkedIn post data if available
            if post_text and len(post_text) > 50:  # Only for longer content
                json_pattern = r'```json\s*\n?(.*?)\n?```'
                json_match = re.search(json_pattern, raw_content, re.DOTALL)
                
                if json_match:
                    try:
                        json_content = json_match.group(1).strip()
                        parsed_json = json.loads(json_content)
                        
                        if 'LinkedInPost' in parsed_json:
                            linkedin_post = parsed_json['LinkedInPost']
                            
                            # Decode escaped characters
                            decoded_post = linkedin_post.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
                            
                            # Extract title - look for first question or statement that ends with punctuation
                            # Try multiple patterns to find a good title break
                            title = ""
                            body = decoded_post
                            
                            # Pattern 1: Question ending with ? and emoji
                            title_match = re.match(r'^([^?]*\? [🤯🚀💡⚡🌟💼🔮🎯📈💻🤖⭐🎨🌈✨]+)', decoded_post.strip())
                            if title_match:
                                title = title_match.group(1).strip()
                                body = decoded_post[len(title):].strip()
                            else:
                                # Pattern 2: First sentence ending with punctuation
                                title_match = re.match(r'^([^.!?\n]*[.!?])', decoded_post.strip())
                                if title_match:
                                    title = title_match.group(1).strip()
                                    body = decoded_post[len(title):].strip()
                                else:
                                    # Pattern 3: First line break
                                    lines = decoded_post.split('\n', 1)
                                    if len(lines) > 1:
                                        title = lines[0].strip()
                                        body = lines[1].strip()
                                    else:
                                        # Fallback: first 100 chars as title
                                        title = decoded_post[:100].strip() + "..."
                                        body = decoded_post
                            
                            # Clean title of hashtags for display (keep original in fullContent)
                            clean_title = re.sub(r'#\w+', '', title).strip()
                            if not clean_title:
                                clean_title = title  # Keep original if cleaning removes everything
                            
                            response_data["data"]["linkedInPost"] = {
                                "title": clean_title,
                                "body": body,
                                "fullContent": decoded_post,
                                "content": decoded_post,  # For backward compatibility
                                "hashtags": re.findall(r'#\w+', decoded_post),
                                "rawContent": linkedin_post  # Original with escape characters
                            }
                            
                            logger.info(f"Extracted LinkedIn post title: {title}")
                    
                    except Exception as e:
                        logger.warning(f"Failed to parse LinkedIn post data: {e}")
            
            # Add binary section for n8n compatibility
            if image_data:
                response_data["binary"] = {
                    "image": {
                        "data": image_data["base64"],
                        "mimeType": image_data["contentType"],
                        "fileName": f"generated_image_{int(time.time())}.png"
                    }
                }
            
            return jsonify(response_data)
        else:
            return jsonify({
                "success": False, 
                "message": "Failed to generate image", 
                "prompt": post_text,
                "error": "GENERATION_FAILED"
            }), 500
    
    except Exception as e:
        logger.error(f"Error in raw image generation: {str(e)}")
        return jsonify({
            "success": False, 
            "message": f"Server error: {str(e)}", 
            "error": "INTERNAL_ERROR"
        }), 500

@app.route('/generate-image', methods=['POST'])
def generate_image():
    """
    Generate an image using Perplexity AI through Playwright browser automation.
    
    Expected JSON body:
    {
        "postText": "A robot helping a human plant a tree."
    }
    OR
    {
        "input": {
            "prompt": "...",
            "aspect_ratio": "1:1",
            "raw": true,
            "output_format": "jpg",
            "safety_tolerance": 6
        }
    }
    
    Returns:
    {
        "success": true,
        "data": {
            "imageUrl": "https://user-gen-media-assets.s3.amazonaws.com/...",
            "imageData": {...}
        }
    }
    """
    try:
        # Handle both JSON and raw content
        data = None
        post_text = None
        
        # Try to parse as JSON first
        try:
            if request.is_json:
                data = request.get_json()
            else:
                # Handle raw content that might not be valid JSON
                raw_content = request.get_data(as_text=True)
                logger.info(f"Received raw content (first 200 chars): {raw_content[:200]}...")
                
                # Try to extract the content manually
                if '"prompt":' in raw_content:
                    # Extract the prompt value from malformed JSON
                    import re
                    prompt_match = re.search(r'"prompt":\s*"([^"]*(?:\\.[^"]*)*)"', raw_content, re.DOTALL)
                    if prompt_match:
                        post_text = prompt_match.group(1).replace('\\"', '"').replace('\\n', '\n')
                        logger.info(f"Extracted prompt from raw content: {post_text[:100]}...")
                else:
                    # Treat entire content as prompt
                    post_text = raw_content
                    
        except Exception as json_error:
            logger.warning(f"JSON parsing failed, trying raw content: {json_error}")
            raw_content = request.get_data(as_text=True)
            post_text = raw_content
        
        # Extract post_text from data if we successfully parsed JSON
        if data and not post_text:
            # Support multiple formats for maximum n8n compatibility
            # Format 1: Direct postText
            if 'postText' in data:
                post_text = data.get('postText')
            
            # Format 2: Nested input.prompt
            elif 'input' in data and isinstance(data['input'], dict):
                post_text = data['input'].get('prompt')
            
            # Format 3: Direct prompt field (for simpler n8n setup)
            elif 'prompt' in data:
                post_text = data.get('prompt')
        
        if not post_text:
            return jsonify({
                "success": False, 
                "message": "Could not extract prompt from request. Provide 'postText', 'input.prompt', or 'prompt' field", 
                "error": "MISSING_PARAMETER",
                "supportedFormats": [
                    {"postText": "your prompt here"},
                    {"input": {"prompt": "your prompt here", "aspect_ratio": "1:1"}},
                    {"prompt": "your prompt here"}
                ]
            }), 400
        
        logger.info(f"Received image generation request")
        logger.info(f"Raw prompt (first 100 chars): {post_text[:100]}...")
        
        # Clean the prompt text if it contains JSON markdown
        cleaned_prompt = post_text
        if '```json' in cleaned_prompt.lower():
            # Extract just the content we want
            try:
                import re
                import json as json_lib
                
                # Extract JSON content between ```json and ```
                json_match = re.search(r'```json\s*\n?(.*?)\n?```', cleaned_prompt, re.DOTALL)
                if json_match:
                    json_content = json_match.group(1).strip()
                    logger.info(f"Extracted JSON content (first 100 chars): {json_content[:100]}...")
                    
                    # Try to parse as JSON to extract ImagePrompt or LinkedInPost
                    parsed = json_lib.loads(json_content)
                    
                    if 'ImagePrompt' in parsed and parsed['ImagePrompt'].strip():
                        cleaned_prompt = parsed['ImagePrompt']
                        logger.info("Using ImagePrompt from parsed JSON")
                    elif 'LinkedInPost' in parsed and parsed['LinkedInPost'].strip():
                        # Create a shorter prompt for image generation from LinkedIn post
                        linkedin_text = parsed['LinkedInPost']
                        # Extract key themes and concepts for image generation
                        if 'AI' in linkedin_text and 'job' in linkedin_text.lower():
                            cleaned_prompt = "Create a professional image showing the future of work with AI, depicting human purpose and identity in technological change"
                        else:
                            # Truncate to a reasonable length for image generation
                            linkedin_summary = linkedin_text[:150] + "..." if len(linkedin_text) > 150 else linkedin_text
                            cleaned_prompt = f"Create a professional image representing: {linkedin_summary}"
                        logger.info("Using condensed LinkedIn post theme for image generation")
                    else:
                        # Use a generic professional image prompt
                        cleaned_prompt = "Create a professional business-themed image with modern, clean design"
                        logger.info("No specific fields found, using generic professional prompt")
                else:
                    logger.warning("Could not extract JSON from markdown, creating generic prompt")
                    if 'linkedin' in post_text.lower():
                        cleaned_prompt = "Create a professional LinkedIn-style image with business themes"
                    else:
                        cleaned_prompt = "Create a professional image with clean, modern design"
            except Exception as e:
                logger.warning(f"Could not parse JSON content: {e}")
                # Fallback: create a short, manageable prompt
                if 'AI' in post_text and ('job' in post_text.lower() or 'work' in post_text.lower()):
                    cleaned_prompt = "Create an image showing AI and the future of work, with human elements and technology"
                elif 'linkedin' in post_text.lower():
                    cleaned_prompt = "Create a professional business image suitable for LinkedIn"
                else:
                    cleaned_prompt = "Create a professional, modern image with clean design"
        
        # Do not forcibly truncate prompts to 300 chars; allow full prompt but cap at a high safety limit
        MAX_PROMPT_LENGTH = 8000
        if len(cleaned_prompt) > MAX_PROMPT_LENGTH:
            cleaned_prompt = cleaned_prompt[:MAX_PROMPT_LENGTH]
            logger.info(f"Truncated prompt to {MAX_PROMPT_LENGTH} characters for safety")
        
        logger.info(f"Final prompt (first 100 chars): {cleaned_prompt[:100]}...")
        
        # Extract additional parameters from input if available
        original_data = request.get_json() or {}
        input_params = original_data.get('input', {}) if isinstance(original_data.get('input'), dict) else {}
        aspect_ratio = input_params.get('aspect_ratio', '1:1')
        output_format = input_params.get('output_format', 'png')
        raw = input_params.get('raw', False)
        safety_tolerance = input_params.get('safety_tolerance', 6)
        
        # Run the Playwright automation asynchronously
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            image_url = loop.run_until_complete(generate_image_with_playwright(cleaned_prompt))
            
            if image_url:
                logger.info(f"Successfully generated image: {image_url}")
                
                # Download and encode the image
                image_data = download_and_encode_image(image_url)
                
                if image_data:
                    # Extract LinkedIn post data if available
                    linkedin_data = None
                    if post_text and len(post_text) > 50:  # Only for longer content
                        json_pattern = r'```json\s*\n?(.*?)\n?```'
                        json_match = re.search(json_pattern, post_text, re.DOTALL)
                        
                        if json_match:
                            try:
                                json_content = json_match.group(1).strip()
                                parsed_json = json.loads(json_content)
                                
                                if 'LinkedInPost' in parsed_json:
                                    linkedin_post = parsed_json['LinkedInPost']
                                    
                                    # Decode escaped characters
                                    decoded_post = linkedin_post.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
                                    
                                    # Extract title - look for first question or statement that ends with punctuation
                                    title = ""
                                    body = decoded_post
                                    
                                    # Pattern 1: Question ending with ? and emoji
                                    title_match = re.match(r'^([^?]*\? [🤯🚀💡⚡🌟💼🔮🎯📈💻🤖⭐🎨🌈✨]+)', decoded_post.strip())
                                    if title_match:
                                        title = title_match.group(1).strip()
                                        body = decoded_post[len(title):].strip()
                                    else:
                                        # Pattern 2: First sentence ending with punctuation
                                        title_match = re.match(r'^([^.!?\n]*[.!?])', decoded_post.strip())
                                        if title_match:
                                            title = title_match.group(1).strip()
                                            body = decoded_post[len(title):].strip()
                                        else:
                                            # Pattern 3: First line break
                                            lines = decoded_post.split('\n', 1)
                                            if len(lines) > 1:
                                                title = lines[0].strip()
                                                body = lines[1].strip()
                                            else:
                                                # Fallback: first 100 chars as title
                                                title = decoded_post[:100].strip() + "..."
                                                body = decoded_post
                                    
                                    # Clean title of hashtags for display
                                    clean_title = re.sub(r'#\w+', '', title).strip()
                                    if not clean_title:
                                        clean_title = title
                                    
                                    linkedin_data = {
                                        "title": clean_title,
                                        "body": body,
                                        "fullContent": decoded_post,
                                        "content": decoded_post,  # For backward compatibility
                                        "hashtags": re.findall(r'#\w+', decoded_post),
                                        "rawContent": linkedin_post  # Original with escape characters
                                    }
                                    
                                    logger.info(f"Extracted LinkedIn post title: {clean_title}")
                            
                            except Exception as e:
                                logger.warning(f"Failed to parse LinkedIn post data: {e}")
                    
                    # n8n compatible format
                    response_data = {
                        "success": True,
                        "message": "Image generated successfully",
                        "data": {
                            "prompt": cleaned_prompt,
                            "originalPrompt": post_text,
                            "imageUrl": image_url,
                            "image": {
                                "filename": f"generated_image_{int(time.time())}.png",
                                "mimeType": image_data["contentType"],
                                "data": image_data["base64"],
                                "size": image_data["size"]
                            },
                            "parameters": {
                                "aspect_ratio": aspect_ratio,
                                "raw": raw,
                                "output_format": output_format,
                                "safety_tolerance": safety_tolerance
                            },
                            "timestamp": datetime.now().isoformat()
                        },
                        "binary": {
                            "image": {
                                "data": image_data["base64"],
                                "mimeType": image_data["contentType"],
                                "fileName": f"generated_image_{int(time.time())}.png"
                            }
                        }
                    }
                    
                    # Add LinkedIn data if extracted
                    if linkedin_data:
                        response_data["data"]["linkedInPost"] = linkedin_data
                    
                    return jsonify(response_data)
                else:
                    # Fallback to URL only if download fails
                    logger.warning("Failed to download image, returning URL only")
                    return jsonify({
                        "success": True,
                        "message": "Image generated but download failed",
                        "data": {
                            "prompt": post_text,
                            "imageUrl": image_url
                        },
                        "warning": "Image download failed, URL provided instead"
                    })
            else:
                logger.error("Failed to generate image")
                return jsonify({
                    "success": False,
                    "message": "Failed to generate image - no image URL returned",
                    "error": "IMAGE_GENERATION_FAILED"
                }), 500
                
        finally:
            loop.close()
            
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({"success": False, "message": "Internal server error", "error": "UNEXPECTED_ERROR", "details": str(e)}), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({"success": False, "message": "Endpoint not found", "error": "NOT_FOUND"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"success": False, "message": "Internal server error", "error": "INTERNAL_ERROR"}), 500

def initialize_brave_connection():
    """Initialize Brave connection on server startup"""
    debug_port = int(os.getenv('BRAVE_DEBUG_PORT', '9222'))
    cdp_url = f"http://127.0.0.1:{debug_port}"
    
    # Check if Brave is already running with remote debugging
    try:
        resp = requests.get(cdp_url.rstrip("/") + "/json", timeout=1)
        if resp.status_code == 200:
            logger.info(f"Brave remote-debugging already reachable on port {debug_port}")
            return True
    except Exception:
        pass
    
    # If not reachable, try to launch Brave
    if sys.platform.startswith("win"):
        logger.info("Launching Brave with remote debugging...")
        launch_brave_windows()
        
        # Wait for CDP to become available
        if wait_for_cdp(cdp_url, timeout_s=20):
            logger.info(f"Successfully connected to Brave CDP at {cdp_url}")
            return True
        else:
            logger.warning(f"Failed to connect to Brave CDP at {cdp_url}")
            return False
    
    return False

if __name__ == '__main__':
    logger.info("Starting Perplexity Image Generator Server...")
    logger.info("Available endpoints:")
    logger.info("  GET  /health - Health check")
    logger.info("  POST /generate-image - Generate image via Perplexity")
    
    # Initialize Brave connection on startup
    initialize_brave_connection()
    
    app.run(
        host='0.0.0.0',
        port=int(os.getenv('PORT', 5000)),
        debug=os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    )
