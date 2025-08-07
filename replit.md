# Overview

This is a Flask-based Universal Prompt Optimizer that transforms rough creative ideas into professional, platform-ready prompts for multiple AI generation services. The application uses heuristic analysis to categorize and enhance user input, generating optimized prompts for SDXL, ComfyUI, Midjourney v6, Pika Labs, and Runway ML. The system follows strict prompt structure ordering (quality → subject → style → lighting → composition → mood → color grade → extra tags) and includes comprehensive negative prompting, platform-specific configuration hints, database-backed API key authentication with expiry dates, daily usage quotas with individual user limits, Stripe webhook integration for automated key provisioning, and comprehensive admin endpoints for manual key management.

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## Backend Framework
The application uses Flask as the web framework, providing a lightweight and flexible foundation for the prompt enhancement service. Flask was chosen for its simplicity and rapid development capabilities, making it ideal for this focused API service.

## Prompt Enhancement Engine
The core functionality uses a sophisticated heuristics-based system that follows industry best practices for AI prompt engineering. Key features:

- **Strict Ordering**: Enforces quality → subject → style → lighting → composition → mood → color grade → extra tags structure for optimal results
- **Platform Optimization**: Generates format-specific outputs for SDXL, ComfyUI, Midjourney v6, Pika Labs, and Runway ML
- **Character Limits**: Automatically clamps prompts to 800-850 characters to prevent quality degradation
- **Keyword Categories**: Photography techniques, art styles, quality descriptors, mood settings, and composition rules
- **Deduplication**: Removes conflicting styles and duplicate terms while preserving order

## Negative Prompt Management
The application includes a comprehensive negative prompting system that automatically filters out common artifacts and unwanted elements. The negative defaults are categorized into:

- Anatomical and structural issues
- Image quality artifacts
- Branding and text elements
- Style-related problems

## Configuration Management
Environment-based configuration is implemented for sensitive settings like session secrets, with fallback defaults for development environments. This ensures security in production while maintaining ease of development.

### Required Environment Variables
- **SESSION_SECRET**: Flask session security key
- **USAGE_DB**: SQLite database path (defaults to "usage.db")
- **ADMIN_TOKEN**: Admin authentication token for management endpoints
- **STRIPE_API_KEY**: Stripe API key for payment processing (optional)
- **STRIPE_WEBHOOK_SECRET**: Stripe webhook signing secret (optional)
- **PUBLIC_BASE_URL**: Base URL for success/cancel redirects (e.g., "https://your-app.onreplit.app")
- **FROM_EMAIL**: Email address for sending API keys to customers

### Email Configuration (choose one)
**SendGrid (recommended):**
- **SENDGRID_API_KEY**: SendGrid API key for email delivery

**SMTP fallback:**
- **SMTP_HOST**: SMTP server hostname
- **SMTP_PORT**: SMTP server port (default: 587)
- **SMTP_USER**: SMTP username
- **SMTP_PASS**: SMTP password

### Stripe Integration Setup
Configure PLAN_MAP in app.py with your Stripe Price IDs:
```python
PLAN_MAP = {
    "price_basic123": {"plan":"basic","daily_limit":50,"days_valid":30},
    "price_pro456":   {"plan":"pro","daily_limit":200,"days_valid":30},
}
```

## API Structure
The service provides comprehensive endpoints for prompt optimization and image generation:

- **Web Interface** (`/`): Interactive dark-themed UI with advanced controls, presets, complete history management, and database-backed API key authentication
- **Optimization API** (`/optimize`, `/api/optimize`): JSON endpoints accepting `{idea, negative, aspect_ratio, lighting, color_grade, extra_tags}` and returning complete platform configurations
- **ComfyUI Generation** (`/generate/comfy`, `/generate/comfy_async`): Direct and async image generation with parameter overrides, workflow customization, and per-key API protection
- **Authentication System** (`/auth/check`, `/usage`, `/usage/charge`): Database-backed API key validation with expiry dates, individual daily quota tracking, and usage management
- **Stripe Integration** (`/stripe/webhook`): Automated API key provisioning for checkout completions and subscription payments with configurable plan mapping and email notifications
- **Checkout System** (`/checkout/create`, `/buy`): Stripe Checkout integration with dedicated buy page for seamless payment flow and instant API key delivery
- **Shareable Links** (`/share/create`, `/s/<token>`, `/share/delete`): Public link generation for sharing generations with expiry dates and parameter display
- **Admin Management** (`/admin/issue`, `/admin/revoke`, `/admin/update_limit`, `/admin/keys`): Manual API key operations for customer support and key lifecycle management
- **Status Polling** (`/generate/comfy_status`): Real-time generation progress tracking for async workflows with authentication
- **ZIP Downloads** (`/zip`): Bulk image packaging accepting image URLs and returning compressed archives
- **Response Format**: Returns unified prompts plus platform-specific configurations (SDXL settings, ComfyUI workflows, Midjourney flags, video motion parameters)
- **Advanced Controls**: Steps, CFG scale, sampler selection, seed management, and batch size controls (1-8 images)
- **History System**: Local storage with up to 200 generation records, complete with re-run capabilities and bulk downloads
- **Progress Tracking**: Real-time progress bars with cancellation capabilities for long-running operations
- **Individual Quotas**: Per-key daily generation limits with automatic usage charging and remaining quota display
- **Key Management**: Database-backed keys with email association, plan tiers, expiry dates, and revocation capabilities
- **Shareable Links**: Public link generation for any generation with 30-day expiry, automatic clipboard copy, and parameter display
- **Bootstrap Demo**: Auto-created demo123 key with 50 daily generations for immediate testing
- **Error Handling**: Comprehensive validation with loading states and user-friendly error messages
- **Execution Hints**: Built-in troubleshooting guidance for common generation issues (faces, motion warping, busy outputs)

# External Dependencies

## Core Framework
- **Flask**: Web application framework for handling HTTP requests and responses
- **SQLite3**: Database backend for API key authentication and daily usage quota tracking
- **Python Standard Library**: Utilizes `re` for pattern matching, `json` for data serialization, `os` for environment variables, `datetime` for quota management, and `textwrap` for text formatting

## Runtime Environment
- **Python 3.x**: Runtime environment for the application
- **Flask Development Server**: Built-in server for development and testing

## Potential Integrations
The architecture is designed to integrate with AI image generation services that accept text prompts, though no specific external APIs are currently implemented in the codebase. The prompt enhancement output format suggests compatibility with popular AI art generation platforms.