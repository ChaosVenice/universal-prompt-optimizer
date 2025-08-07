# Overview

This is a Flask-based Universal Prompt Optimizer that transforms rough creative ideas into professional, platform-ready prompts for multiple AI generation services. The application uses heuristic analysis to categorize and enhance user input, generating optimized prompts for SDXL, ComfyUI, Midjourney v6, Pika Labs, and Runway ML. The system follows strict prompt structure ordering (quality → subject → style → lighting → composition → mood → color grade → extra tags) and includes comprehensive negative prompting and platform-specific configuration hints.

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

## API Structure
The service provides two endpoints:

- **Web Interface** (`/`): Interactive dark-themed UI with form inputs for idea, negative prompts, aspect ratios, and optional enhancements
- **Direct API** (`/api/optimize`): JSON-only endpoint accepting `{idea, negative, aspect_ratio}` and returning complete platform configurations
- **Response Format**: Returns unified prompts plus platform-specific configurations (SDXL settings, ComfyUI node hints, Midjourney flags, video motion parameters)
- **Error Handling**: Comprehensive validation with loading states and user-friendly error messages
- **Execution Hints**: Built-in troubleshooting guidance for common generation issues (faces, motion warping, busy outputs)

# External Dependencies

## Core Framework
- **Flask**: Web application framework for handling HTTP requests and responses
- **Python Standard Library**: Utilizes `re` for pattern matching, `json` for data serialization, `os` for environment variables, and `textwrap` for text formatting

## Runtime Environment
- **Python 3.x**: Runtime environment for the application
- **Flask Development Server**: Built-in server for development and testing

## Potential Integrations
The architecture is designed to integrate with AI image generation services that accept text prompts, though no specific external APIs are currently implemented in the codebase. The prompt enhancement output format suggests compatibility with popular AI art generation platforms.