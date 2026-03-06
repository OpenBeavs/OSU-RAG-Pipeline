#!/usr/bin/env python3
"""
Universal A2A Agent Deployment Script for Cloud Run

This script deploys ADK A2A agents to Google Cloud Run.
It automatically pulls configuration from your gcloud config and constructs
the proper gcloud run deploy command.

Usage:
    python deploy_agent.py <agent-name> [options]

Examples:
    python deploy_agent.py oregon-state-expert
    python deploy_agent.py Cyrano-de-Bergerac --project my-project
    python deploy_agent.py oregon-state-expert --region us-central1
"""

import subprocess
import sys
import os
import argparse
import shutil
from pathlib import Path
from dotenv import load_dotenv


def _gcloud_exe() -> str:
    """Return the correct gcloud executable name for the current platform."""
    # On Windows gcloud is installed as gcloud.cmd
    for name in ("gcloud", "gcloud.cmd"):
        path = shutil.which(name)
        if path:
            return path
    return "gcloud"


def get_gcloud_config(property_name):
    """Get a property from gcloud config."""
    try:
        result = subprocess.run(
            [_gcloud_exe(), "config", "get-value", property_name],
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        value = result.stdout.strip()
        if value and value != "(unset)":
            return value
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return None


def get_project_number(project_id):
    """Get the project number for a given project ID."""
    try:
        result = subprocess.run(
            [_gcloud_exe(), "projects", "describe", project_id, "--format=value(projectNumber)"],
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Deploy an A2A agent to Google Cloud Run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s oregon-state-expert
  %(prog)s Cyrano-de-Bergerac --project my-project
  %(prog)s oregon-state-expert --region us-central1
        """
    )
    
    parser.add_argument(
        'agent_name',
        help='Name of the agent directory to deploy'
    )
    
    parser.add_argument(
        '--project',
        help='GCP project ID (default: from gcloud config)'
    )
    
    parser.add_argument(
        '--region',
        help='GCP region (default: from gcloud config or us-west1)'
    )
    
    parser.add_argument(
        '--allow-unauthenticated',
        action='store_true',
        help='Make the service public (default: requires authentication)'
    )
    
    parser.add_argument(
        '--memory',
        default='1Gi',
        help='Memory allocation (default: 1Gi)'
    )
    
    args = parser.parse_args()

    # Load .env from project root so GOOGLE_API_KEY etc. are available
    env_path = Path(__file__).parent / ".env"
    load_dotenv(env_path)

    # Auto-detect project from gcloud config
    project = args.project or get_gcloud_config("project")
    if not project:
        print("\n✗ Error: No GCP project specified and could not detect from gcloud config.")
        print("   Please either:")
        print("   1. Set default project: gcloud config set project YOUR-PROJECT-ID")
        print("   2. Use --project flag: python deploy_agent.py <agent> --project YOUR-PROJECT-ID")
        sys.exit(1)

    # Auto-detect region from gcloud config
    region = args.region or get_gcloud_config("compute/region") or "us-west1"

    # Sanitize service name: Cloud Run requires lowercase alphanumeric + dashes only
    # Strip any leading/trailing path separators, then replace underscores with dashes
    raw_name = Path(args.agent_name).name  # handles e.g. .\osu_rag_agent\
    service_name = raw_name.replace("_", "-").lower()

    # Get project number for APP_URL
    project_number = get_project_number(project)
    if not project_number:
        print(f"\n⚠ Warning: Could not get project number for {project}")
        print("   APP_URL will need to be updated manually after deployment")
        app_url = f"https://{service_name}.{region}.run.app"
    else:
        app_url = f"https://{service_name}-{project_number}.{region}.run.app"

    # Determine agent directory (always resolve from the raw input)
    script_dir = Path(__file__).parent.resolve()
    agent_dir = (script_dir / args.agent_name).resolve()
    
    if not agent_dir.exists() or not agent_dir.is_dir():
        print(f"\n✗ Error: Agent directory not found: {agent_dir}")
        print(f"   Available agents: {', '.join([d.name for d in script_dir.iterdir() if d.is_dir() and not d.name.startswith('.')])}")
        sys.exit(1)
    
    print(f"\n{'='*70}")
    print("A2A Agent Cloud Run Deployment")
    print(f"{'='*70}")
    print(f"Agent dir:      {args.agent_name}")
    print(f"Service name:   {service_name}")
    print(f"Project:        {project}")
    print(f"Region:         {region}")
    print(f"Memory:         {args.memory}")
    print(f"Authentication: Public (allUsers)")
    print(f"App URL:        {app_url}")
    print(f"Source:         {agent_dir}")
    print(f"{'='*70}\n")
    
    # Collect extra env vars from .env (add only non-empty ones the agent needs)
    google_api_key       = os.environ.get("GOOGLE_API_KEY", "")
    firestore_collection = os.environ.get("FIRESTORE_COLLECTION", "osu-knowledge")
    gcp_project_id       = os.environ.get("GCP_PROJECT_ID", project)

    env_vars = {
        "GOOGLE_CLOUD_PROJECT":    project,
        "GOOGLE_CLOUD_LOCATION":   region,
        "GCP_PROJECT_ID":          gcp_project_id,
        "FIRESTORE_COLLECTION":    firestore_collection,
        "APP_URL":                 app_url,
        "HOST_OVERRIDE":           app_url,
    }
    if google_api_key:
        env_vars["GOOGLE_API_KEY"] = google_api_key

    env_vars_str = ",".join(f"{k}={v}" for k, v in env_vars.items())

    # Construct the gcloud run deploy command
    deploy_cmd = [
        _gcloud_exe(), "run", "deploy", service_name,
        "--quiet",          # suppress interactive prompts (e.g. --allow-unauthenticated warning)
        "--port=8080",
        f"--source={agent_dir}",
        f"--region={region}",
        f"--project={project}",
        f"--memory={args.memory}",
        f"--set-env-vars={env_vars_str}",
    ]

    # A2A agents require public access so other agents can discover the agent card.
    deploy_cmd.append("--allow-unauthenticated")
    
    print("Running command:")
    print(" ".join(deploy_cmd))
    print("\nThis may take several minutes...\n")
    
    # Run the deployment
    # Use shell=False so Python handles path quoting correctly
    # (avoids splitting paths with spaces when shell=True on Windows)
    try:
        result = subprocess.run(
            deploy_cmd,
            text=True,
            encoding='utf-8'
        )
        
        if result.returncode == 0:
            # ── Re-apply public IAM binding (gcloud run deploy resets it) ─
            print("\nApplying public IAM binding for A2A agent card endpoint...")
            iam_cmd = [
                _gcloud_exe(), "run", "services", "add-iam-policy-binding",
                service_name,
                f"--region={region}",
                f"--project={project}",
                "--member=allUsers",
                "--role=roles/run.invoker",
            ]
            subprocess.run(iam_cmd, text=True, encoding='utf-8')

            print(f"\n{'='*70}")
            print("✓ Agent deployment successful!")
            print(f"{'='*70}")
            print(f"\nYour A2A agent is now deployed to Cloud Run!")
            agent_card_url = f"{app_url}/a2a/{raw_name}/.well-known/agent-card.json"
            agent_rpc_url  = f"{app_url}/a2a/{raw_name}/"
            print(f"\n🌐 Service URL:   {app_url}")
            print(f"\n📋 A2A Agent Card: {agent_card_url}")
            print(f"📡 A2A RPC:        {agent_rpc_url}")
            print(f"\nTo test your agent:")
            print(f"  Invoke-WebRequest \"{agent_card_url}\" | Select-Object -ExpandProperty Content")
            print(f"\nTo view logs:")
            print(f"  gcloud run services logs read {service_name} --project={project} --region={region}")
            print(f"\nTo update environment variables:")
            print(f"  gcloud run services update {service_name} --update-env-vars KEY=VALUE --project={project} --region={region}\n")
        else:
            print(f"\n{'='*70}")
            print("✗ Agent deployment FAILED")
            print(f"{'='*70}")
            print("\nCheck the error messages above for details.")
            print("\nCommon issues:")
            print("  - Missing Dockerfile or requirements.txt in agent directory")
            print("  - Insufficient permissions")
            print("  - Invalid source code structure")
            print(f"\nFor more help, see: https://cloud.google.com/run/docs/deploy-a2a-agents")
            sys.exit(1)

            
    except FileNotFoundError:
        print(f"\n✗ Error: 'gcloud' command not found.")
        print("\nPlease ensure Google Cloud SDK is installed:")
        print("  Visit: https://cloud.google.com/sdk/docs/install")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n✗ Deployment cancelled by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
