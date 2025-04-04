import os
import subprocess
import sys
import tempfile
import uuid
import webbrowser
from collections.abc import Mapping
from contextlib import ExitStack
from enum import Enum
from pathlib import Path
from typing import Optional

import click
import jinja2
from dagster_shared.plus.config import DagsterPlusCliConfig
from dagster_shared.plus.login_server import start_login_server

from dagster_dg.cli.shared_options import dg_editable_dagster_options, dg_global_options
from dagster_dg.cli.utils import create_temp_dagster_cloud_yaml_file
from dagster_dg.config import normalize_cli_config
from dagster_dg.context import DgContext
from dagster_dg.env import ProjectEnvVars
from dagster_dg.utils import DgClickCommand, DgClickGroup
from dagster_dg.utils.git import get_local_branch_name
from dagster_dg.utils.plus.gql import FULL_DEPLOYMENTS_QUERY, SECRETS_QUERY
from dagster_dg.utils.plus.gql_client import DagsterCloudGraphQLClient
from dagster_dg.utils.telemetry import cli_telemetry_wrapper


@click.group(name="plus", cls=DgClickGroup, hidden=True)
def plus_group():
    """Commands for interacting with Dagster Plus."""


@plus_group.command(name="login", cls=DgClickCommand)
@cli_telemetry_wrapper
def login_command() -> None:
    """Login to Dagster Plus."""
    server, url = start_login_server()

    try:
        webbrowser.open(url, new=0, autoraise=True)
        click.echo(
            f"Opening browser...\nIf a window does not open automatically, visit {url} to"
            " finish authorization"
        )
    except webbrowser.Error as e:
        click.echo(f"Error launching web browser: {e}\n\nTo finish authorization, visit {url}\n")

    server.serve_forever()
    new_org = server.get_organization()
    new_api_token = server.get_token()

    config = DagsterPlusCliConfig(
        organization=new_org,
        user_token=new_api_token,
        url=DagsterPlusCliConfig.get().url if DagsterPlusCliConfig.exists() else None,
    )
    config.write()
    click.echo(f"Authorized for organization {new_org}\n")

    gql_client = DagsterCloudGraphQLClient.from_config(config)
    result = gql_client.execute(FULL_DEPLOYMENTS_QUERY)
    deployment_names = [d["deploymentName"] for d in result["fullDeployments"]]

    click.echo("Available deployments: " + ", ".join(deployment_names))

    selected_deployment = None
    while selected_deployment not in deployment_names:
        if selected_deployment is not None:
            click.echo(f"{selected_deployment} is not a valid deployment")
        selected_deployment = click.prompt(
            "Select a default deployment", default=deployment_names[0]
        )

    config = DagsterPlusCliConfig(
        organization=config.organization,
        user_token=config.user_token,
        default_deployment=selected_deployment,
        url=config.url,
    )
    config.write()


# ########################
# ##### PLUS ENV MANAGEMENT
# ########################


@plus_group.group(name="pull", cls=DgClickGroup)
def plus_pull_group():
    """Commands for pulling configuration from Dagster Plus."""


def _get_local_secrets_for_locations(
    client: DagsterCloudGraphQLClient, location_names: set[str]
) -> Mapping[str, Mapping[str, str]]:
    secrets_by_location = {location_name: {} for location_name in location_names}

    result = client.execute(
        SECRETS_QUERY, variables={"onlyViewable": True, "scopes": {"localDeploymentScope": True}}
    )
    for secret in result["secretsOrError"]["secrets"]:
        if not secret["localDeploymentScope"]:
            continue
        for location_name in location_names:
            if len(secret["locationNames"]) == 0 or location_name in secret["locationNames"]:
                secrets_by_location[location_name][secret["secretName"]] = secret["secretValue"]

    return secrets_by_location


@plus_pull_group.command(name="env", cls=DgClickCommand)
@dg_global_options
@cli_telemetry_wrapper
def pull_env_command(**global_options: object) -> None:
    """Pull environment variables from Dagster Plus and save to a .env file for local use."""
    cli_config = normalize_cli_config(global_options, click.get_current_context())

    dg_context = DgContext.for_workspace_or_project_environment(Path.cwd(), cli_config)
    if not DagsterPlusCliConfig.exists():
        raise click.UsageError(
            "`dg plus pull env` requires authentication with Dagster Plus. Run `dg plus login` to authenticate."
        )
    config = DagsterPlusCliConfig.get()

    project_ctxs = []
    if dg_context.is_workspace:
        project_ctxs = [
            dg_context.for_project_environment(project_spec.path, cli_config)
            for project_spec in dg_context.project_specs
        ]
    else:
        project_ctxs = [dg_context]

    gql_client = DagsterCloudGraphQLClient.from_config(config)
    secrets_by_location = _get_local_secrets_for_locations(
        gql_client, {project_ctx.project_name for project_ctx in project_ctxs}
    )

    projects_without_secrets = {project_ctx.project_name for project_ctx in project_ctxs}
    for project_ctx in project_ctxs:
        if secrets_by_location[project_ctx.project_name]:
            env = ProjectEnvVars.empty(project_ctx).with_values(
                secrets_by_location[project_ctx.project_name]
            )
            env.write()
            projects_without_secrets.remove(project_ctx.project_name)

    if dg_context.is_project:
        click.echo("Environment variables saved to .env")
    else:
        click.echo("Environment variables saved to .env for projects in workspace")
        if projects_without_secrets:
            click.echo(
                f"Environment variables not found for projects: {', '.join(projects_without_secrets)}"
            )


def _create_temp_deploy_dockerfile(dst_path, python_version, use_editable_dagster: bool):
    dockerfile_template_path = (
        Path(__file__).parent.parent
        / "templates"
        / (
            "deploy_uv_editable_Dockerfile.jinja"
            if use_editable_dagster
            else "deploy_uv_Dockerfile.jinja"
        )
    )

    loader = jinja2.FileSystemLoader(searchpath=os.path.dirname(dockerfile_template_path))
    env = jinja2.Environment(loader=loader)

    template = env.get_template(os.path.basename(dockerfile_template_path))

    with open(dst_path, "w", encoding="utf8") as f:
        f.write(template.render(python_version=python_version))
        f.write("\n")


class DgPlusDeploymentType(Enum):
    FULL_DEPLOYMENT = "full"
    BRANCH_DEPLOYMENT = "branch"


class DgPlusAgentType(Enum):
    SERVERLESS = "serverless"
    HYBRID = "hybrid"


def _guess_and_prompt_deployment_type(
    project_dir: Path, full_deployment_name: str, skip_confirmation_prompt: bool
) -> DgPlusDeploymentType:
    deployment_type, branch_name = _guess_deployment_type(project_dir, full_deployment_name)

    if not skip_confirmation_prompt and not click.confirm("Do you want to continue?"):
        click.echo("Deployment cancelled.")
        raise click.Abort()

    return deployment_type


def _guess_deployment_type(
    project_dir: Path, full_deployment_name: str
) -> tuple[DgPlusDeploymentType, str]:
    branch_name = get_local_branch_name(str(project_dir))
    if not branch_name:
        click.echo(f"Could not determine a git branch, so deploying to {full_deployment_name}.")
        return DgPlusDeploymentType.FULL_DEPLOYMENT, full_deployment_name

    if branch_name in {"main", "master"}:
        click.echo(f"Current branch is {branch_name}, so deploying to {full_deployment_name}.")
        return DgPlusDeploymentType.FULL_DEPLOYMENT, full_deployment_name

    click.echo(
        f"Deploying to the branch deployment for {branch_name}, with {full_deployment_name} as the base deployment."
    )
    return DgPlusDeploymentType.BRANCH_DEPLOYMENT, branch_name


def _build_hybrid_image(
    dg_context: DgContext,
    deployment: str,
    dockerfile_path: Path,
    use_editable_dagster: bool,
    statedir: str,
) -> None:
    if not dg_context.build_config:
        raise click.UsageError("No build config found. Please specify a registry in build.yaml.")

    registry = dg_context.build_config["registry"]
    source_directory = dg_context.build_config.get("directory", ".")

    tag = f"{deployment}-{dg_context.code_location_name}-{uuid.uuid4().hex!s}"

    build_cmd = [
        "docker",
        "build",
        source_directory,
        "-t",
        f"{registry}:{tag}",
        "-f",
        dockerfile_path,
        "--platform",
        "linux/amd64",
    ]

    if use_editable_dagster:
        build_cmd += [
            "--build-context",
            f"oss={os.environ['DAGSTER_GIT_REPO_DIR']}",
            "--build-context",
            f"internal={os.environ['DAGSTER_INTERNAL_GIT_REPO_DIR']}",
        ]

    subprocess.run(build_cmd, check=True)

    push_cmd = [
        "docker",
        "push",
        f"{registry}:{tag}",
    ]

    subprocess.run(push_cmd, check=True)

    dg_context.external_dagster_cloud_cli_command(
        [
            "ci",
            "set-build-output",
            "--statedir",
            str(statedir),
            "--location-name",
            dg_context.code_location_name,
            "--image-tag",
            tag,
        ]
    )


@plus_group.command(name="deploy", cls=DgClickCommand)
@click.option(
    "--organization",
    "organization",
    help="Dagster+ organization to which to deploy. If not set, defaults to the value set by `dg plus login`.",
    envvar="DAGSTER_PLUS_ORGANIZATION",
)
@click.option(
    "--deployment",
    "deployment",
    help="Name of the Dagster+ deployment to which to deploy (or use as the base deployment if deploying to a branch deployment). If not set, defaults to the value set by `dg plus login`.",
    envvar="DAGSTER_PLUS_DEPLOYMENT",
)
@click.option(
    "--python-version",
    "python_version",
    type=click.Choice(["3.9", "3.10", "3.11", "3.12"]),
    help=(
        "Python version used to deploy the project. If not set, defaults to the calling process's Python minor version."
    ),
)
@click.option(
    "--deployment-type",
    "deployment_type_str",
    type=click.Choice([deployment_type.value for deployment_type in DgPlusDeploymentType]),
    help="Whether to deploy to a full deployment or a branch deployment. If unset, will attempt to infer from the current git branch.",
)
@click.option(
    "--agent-type",
    "agent_type_str",
    type=click.Choice([agent_type.value for agent_type in DgPlusAgentType]),
    help="Whether this a Hybrid or serverless code location.",
    required=True,
)
@click.option(
    "-y", "--yes", "skip_confirmation_prompt", is_flag=True, help="Skip confirmation prompts."
)
@dg_editable_dagster_options
@dg_global_options
@cli_telemetry_wrapper
def deploy_command(
    organization: Optional[str],
    deployment: Optional[str],
    python_version: Optional[str],
    agent_type_str: str,
    deployment_type_str: Optional[str],
    use_editable_dagster: Optional[str],
    skip_confirmation_prompt: bool,
    **global_options: object,
) -> None:
    """Deploy a project to Dagster Plus."""
    cli_config = normalize_cli_config(global_options, click.get_current_context())

    if not python_version:
        python_version = f"3.{sys.version_info.minor}"

    plus_config = DagsterPlusCliConfig.get()

    organization = organization or plus_config.organization
    if not organization:
        raise click.UsageError(
            "Organization not specified. To specify an organization, use the --organization option "
            "or run `dg plus login`."
        )

    deployment = deployment or plus_config.default_deployment
    if not deployment:
        raise click.UsageError(
            "Deployment not specified. To specify a deployment, use the --deployment option "
            "or run `dg plus login`."
        )

    # TODO This command should work in a workspace context too and apply to multiple projects
    dg_context = DgContext.for_project_environment(Path.cwd(), cli_config)

    # TODO Confirm that dagster-cloud is packaged in the project

    deployment_type = (
        DgPlusDeploymentType(deployment_type_str)
        if deployment_type_str
        else _guess_and_prompt_deployment_type(
            dg_context.root_path, deployment, skip_confirmation_prompt
        )
    )

    # TODO derive this from graphql if it is not set
    agent_type = DgPlusAgentType(agent_type_str)

    with ExitStack() as stack:
        # TODO Once this is split out into multiple commands, we need a default statedir
        # that can be persisted across commands.
        statedir = stack.enter_context(tempfile.TemporaryDirectory())

        # Construct a dagster_cloud.yaml file based on info in the pyproject.toml
        dagster_cloud_yaml_file = stack.enter_context(
            create_temp_dagster_cloud_yaml_file(dg_context)
        )

        dg_context.external_dagster_cloud_cli_command(
            [
                "ci",
                "init",
                "--statedir",
                str(statedir),
                "--dagster-cloud-yaml-path",
                dagster_cloud_yaml_file,
                "--project-dir",
                str(dg_context.root_path),
                "--deployment",
                deployment,
                "--organization",
                organization,
            ]
            + (
                ["--require-branch-deployment"]
                if deployment_type == DgPlusDeploymentType.BRANCH_DEPLOYMENT
                else []
            ),
        )

        dockerfile_path = dg_context.root_path / "Dockerfile"
        if not os.path.exists(dockerfile_path):
            click.echo(f"No Dockerfile found - scaffolding a default one at {dockerfile_path}.")
            _create_temp_deploy_dockerfile(
                dockerfile_path, python_version, bool(use_editable_dagster)
            )
        else:
            click.echo(f"Building using Dockerfile at {dockerfile_path}.")

        if agent_type == DgPlusAgentType.HYBRID:
            _build_hybrid_image(
                dg_context, deployment, dockerfile_path, bool(use_editable_dagster), statedir
            )

        else:
            dg_context.external_dagster_cloud_cli_command(
                [
                    "ci",
                    "build",
                    "--statedir",
                    str(statedir),
                    "--dockerfile-path",
                    str(dg_context.root_path / "Dockerfile"),
                ],
            )

        dg_context.external_dagster_cloud_cli_command(
            [
                "ci",
                "deploy",
                "--statedir",
                str(statedir),
            ],
        )
