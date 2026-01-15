"""Script to list available Google AI models"""
import os
from dotenv import load_dotenv
import google.generativeai as genai

# Load environment variables
load_dotenv()

# Configure API key from environment
api_key = os.getenv("GOOGLE_API_KEY")
print(f"API Key loaded: {api_key[:10]}..." if api_key else "No API key found!")
genai.configure(api_key=api_key)

print("Listing all available models:\n")
print("="*80)

for model in genai.list_models():
    print(f"\nModel: {model.name}")
    print(f"  Display Name: {model.display_name}")
    print(f"  Description: {model.description}")

    # Check what methods are supported
    if hasattr(model, 'supported_generation_methods'):
        methods = model.supported_generation_methods
        print(f"  Supported Methods: {', '.join(methods)}")

    print("-"*80)

print("\n\nModels that support 'generateContent':\n")
print("="*80)

for model in genai.list_models():
    if hasattr(model, 'supported_generation_methods'):
        if 'generateContent' in model.supported_generation_methods:
            print(f"✓ {model.name}")
