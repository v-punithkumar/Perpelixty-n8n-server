# Perplexity AI Image Generator Server

A Flask-based server that automates image generation using Perplexity AI's interface through Playwright. This server is designed to work with n8n workflows for LinkedIn post automation.

## Demo
Watch the demo video to see the server in action:

<video width="100%" autoplay loop muted>
  <source src="https://cdn.githubraw.com/v-punithkumar/Perpelixty-n8n-server/main/perplexity%20server.mp4" type="video/mp4">
  Your browser does not support the video tag.
</video>

## Features

- Generate AI images using Perplexity's interface
- Split LinkedIn posts into title and content
- Process image generation requests with various input formats
- Health check endpoint
- Automated browser management with Brave
- Base64 image encoding for n8n compatibility

## Prerequisites

```txt
nest_asyncio
flask
dotenv
requests
playwright
```

## Installation

1. Clone the repository
2. Install the required dependencies:

```sh
pip install -r requirement.txt
```

3. Install Playwright browsers:

```sh
playwright install
```

4. Install Brave browser if not already installed

## Configuration

The server uses environment variables for configuration:

- `PORT`: Server port (default: 5000)
- `FLASK_DEBUG`: Debug mode (default: False)
- `BRAVE_DEBUG_PORT`: Brave browser debug port (default: 9222)
- `PERPLEXITY_WAIT_MS`: Maximum wait time for image generation (default: 60000)

## API Endpoints

### Health Check
```
GET /health
```

### Generate Image
```
POST /generate-image
```
Request body formats:
```json
{
    "postText": "Your prompt here"
}
```
or
```json
{
    "input": {
        "prompt": "Your prompt here",
        "aspect_ratio": "1:1",
        "raw": true,
        "output_format": "jpg",
        "safety_tolerance": 6
    }
}
```

### Split LinkedIn Post
```
POST /split-linkedin
```
Request body format:
```json
{
    "text": {
        "LinkedInPost": "Your LinkedIn post content",
        "ImagePrompt": "Image generation prompt"
    }
}
```

### Generate Image (Raw)
```
POST /generate-image-raw
```
Accepts raw text content for more flexible integration.

## n8n Integration

This server is designed to work with n8n workflows. The provided workflow file `My workflow.json` demonstrates:

- Scheduled post generation
- API calls to Perplexity
- Image generation
- LinkedIn post creation
- Error handling

## Usage with n8n

1. Import `My workflow.json` into your n8n instance
2. Configure the LinkedIn credentials
3. Update API endpoints to match your server address
4. Enable the workflow

## Error Handling

The server provides detailed error messages and logging for:
- Missing parameters
- Image generation failures
- Invalid JSON
- Server errors
- Browser automation issues