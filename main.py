from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Dict



import os
import shutil
import mlflow
from mlflow.tracking import MlflowClient
import docker
import configparser

import requests

# load config once
config = configparser.ConfigParser()
config.read('/default.cfg')
BASE_IMAGE_NAME = "mlflow-packer-base"


def get_mflow_client():
    token = config.get('Databricks', 'TOKEN')
    registry = config.get('Databricks', 'REGISTRY')
    user = config.get('Databricks', 'USER')

    os.environ['DATABRICKS_HOST'] = registry
    os.environ['DATABRICKS_TOKEN'] = token
    os.environ["MLFLOW_TRACKING_TOKEN"] = token
    os.environ["MLFLOW_TRACKING_INSECURE_TLS"] = "true"
    mlflow.set_tracking_uri(registry)

    return MlflowClient()


def get_repo_tags(repo):

    repo = repo.replace("_", "-")
    
    base_url = config.get('Docker', 'HOST')
    token = config.get('Docker', 'TOKEN')
    user = config.get('Docker', 'USER')
    org = config.get('Docker', 'ORG')

    login_url = f"{base_url}/users/login"
    repo_url = f"{base_url}/repositories/{org}/{repo}/tags"

    tok_req = requests.post(
        login_url, json={"username": user, "password": token})
    token = tok_req.json()["token"]
    headers = {"Authorization": f"JWT {token}"}

    res = requests.get(repo_url, headers=headers)
    data = res.json()

    return [el["name"] for el in data["results"]]




def mlflow_build_docker(source, name, env):
    org = config.get('Docker', 'ORG')
    print(f'mlflow models build-docker -m {source} -n {org}/{name} --env-manager {env}')
    os.system(
        f'mlflow models build-docker -m {source} -n {org}/{name} --env-manager {env}'
    )


def docker_push(name):
    base_url = config.get('Docker', 'HOST')
    token = config.get('Docker', 'TOKEN')
    user = config.get('Docker', 'USER')
    org = config.get('Docker', 'ORG')

    client = docker.from_env()
    client.login(username = user, password=token)

    return client.api.push(f"{org}/{name}")


def docker_pull(name):
    base_url = config.get('Docker', 'HOST')
    token = config.get('Docker', 'TOKEN')
    user = config.get('Docker', 'USER')
    org = config.get('Docker', 'ORG')

    client = docker.from_env()
    client.login(username = user, password=token)

    return client.api.pull(f"{org}/{name}")



def build_mlflow_packer_base(python_version, tag, req_file_name, modeldir):
    """
    create a base image for to serve a model with all the required dependencies
    """

    org = config.get('Docker', 'ORG')

    dockerfile = f"""
FROM python:{python_version}

COPY {modeldir.name}/requirements.txt /tmp/
RUN pip install -r /tmp/requirements.txt \\
    && pip install uvicorn==0.18.2 protobuf==3.20.* fastapi==0.80.*\\
    && mkdir -p /model

WORKDIR /model
EXPOSE 8080

ENTRYPOINT gunicorn main:app --workers 2 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:8080 --timeout 120
    

"""
    
    with open("baseDockerfile", 'w') as f:
        f.write(dockerfile)
    os.system(
        f'docker build -f baseDockerfile -t {org}/{BASE_IMAGE_NAME}:{tag} .' 
    )


    return docker_push(f'{BASE_IMAGE_NAME}:{tag}')
        
            
        
def build_with_base_image(model, version):
    """build the model server without mlflow, but with a good base base image
    """
    
    import tempfile
    import yaml
    import hashlib

    org = config.get('Docker', 'ORG')
    cwd = os.getcwd()

    with tempfile.TemporaryDirectory() as tmpdirname:
        os.chdir(tmpdirname)
        command = f'mlflow artifacts  download -u {version.source} -d {tmpdirname}'
        print(command)
        os.system(command)

        model_dir = list(os.scandir(tmpdirname))
        if len(model_dir) == 1:
            model_dir = model_dir[0]
        else:
            raise Exception("Multiple model dirs downloaded")

        # extract python version
        with open(os.path.join(model_dir, "conda.yaml"), "r") as stream:
            try:
                python_version = [
                    d for d in yaml.safe_load(stream)["dependencies"]
                    if "python" in d
                    ][0]
                python_version = python_version.split("=")[-1]
            except yaml.YAMLError as exc:
                raise Exception("Problem parsing conda.yaml")

        # create requirements hash
        md5_hash = hashlib.md5()
        md5_hash.update(b"24.01.2023")
        req_file_name = os.path.join(model_dir, "requirements.txt") 
        with open(req_file_name,"rb") as f:
            # Read and update hash in chunks of 4K
            for byte_block in iter(lambda: f.read(4096),b""):
                md5_hash.update(byte_block)
        req_hash = md5_hash.hexdigest()

        # check if the matching minimal model container is available
        try:
            known_containers = get_repo_tags(BASE_IMAGE_NAME)
        except:
            known_containers = []

        new_tag = f"{python_version}-{req_hash}"
        
        # compute a new container if needed
        if new_tag not in known_containers:
            res = build_mlflow_packer_base(python_version, new_tag, req_file_name, model_dir)
        else:
            print(f"pull image {BASE_IMAGE_NAME}:{new_tag}")
            docker_pull(f"{BASE_IMAGE_NAME}:{new_tag}")

        # inject main.py
        shutil.copyfile("/app/buildtemplate/main.py", os.path.join(model_dir, "main.py"))
        shutil.copyfile("/app/buildtemplate/setup.py", os.path.join(model_dir, "setup.py"))

        # create dockerfile with the serving
        dockerfile = f"""
        
FROM {org}/{BASE_IMAGE_NAME}:{new_tag}

COPY {model_dir.name}/ /model/
RUN python setup.py

        """
        
        with open("Dockerfile", 'w') as f:
            f.write(dockerfile)

        # build the dockerfile
        new_name = f"{model.name.lower().replace('_', '-')}:{version.version}"
        os.system(
            f'docker build -f Dockerfile -t {org}/{new_name} .' 
        )        

        # publish the container
        res = docker_push(new_name)



    os.chdir(cwd)
    return res




app = FastAPI(
    title="MLflow Packer",
    description="""Build and push mlflow models.""",
    version="0.0.10",
)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url='/docs')



class MlflowList(BaseModel):
    name: str
    latest_versions: Dict[str, str] = None


@app.get("/models",  response_model=List[MlflowList])
async def list_models():
    """
    List the available model versions in mlflow registry:

    - **name**: model name
    - **latest_versions**: set of version and Production, Staging, Archived state
    """


    # extract list of marked tags
    model_tags = [e.strip() for e in config.get("Models", "TAGS", fallback = "").split(",") if e != '']

    # use the mlflow client to get all models
    mlflow_c = get_mflow_client()
    models = [m for m in mlflow_c.list_registered_models() if any(
        [t  in m.tags.keys() for t in model_tags]) or len(model_tags) == 0]

    return JSONResponse([
        {
            "name": m.name,
            "latest_versions": {
                v.version: v.current_stage
                for v in m.latest_versions
             }
            } for m in models])





class DockerList(BaseModel):
    name: str
    versions: List[str]


@app.get("/images", response_model=List[DockerList])
async def list_docker_models():
    """
    List the available model versions in docker regristry:

    - **name**: model name
    - **versions**: list of all versions
    """


    # extract list of marked tags
    model_tags = [e.strip() for e in config.get("Models", "TAGS", fallback = "").split(",") if e != '']

    # use the mlflow client to get all models
    mlflow_c = get_mflow_client()
    models = [m for m in mlflow_c.list_registered_models() if any(
        [t  in m.tags.keys() for t in model_tags]) or len(model_tags) == 0]

    return JSONResponse([
        {
            "name": m.name,
            "versions":  get_repo_tags(m.name)
            } for m in models])






class BuildResponse(BaseModel):
    result: str

@app.get("/build", response_model=BuildResponse)
async def build_docker_model(name: str, version: str, env: str = "baseimage"):
    """
    Build a new model version an push it to the server regitry

    - **name**: model name
    - **version**: the version to build
    - **env**: specify environment manager (local, conda, virtualenv, baseimage)
    """

    # cleanup before start
    os.system("docker system prune -f")

    # use the mlflow client to get all models
    mlflow_c = get_mflow_client()
    models = mlflow_c.list_registered_models()

    model = [m for m in models if m.name == name]

    if len(model) == 0:
        return JSONResponse({"result": "Model not found."})

    model = model[0]

    version = [v for v in model.latest_versions if v.version == version]


    if len(version) == 0:
        return JSONResponse({"result": "Version not found."})

    version = version[0]

    if env == "baseimage":

        res = build_with_base_image(model, version)
        return JSONResponse({"result": res})

    else:

        new_name = f"{model.name.lower().replace('_', '-')}:{version.version}"
        
        mlflow_build_docker(version.source, new_name, env)
        res = docker_push(new_name)

        return JSONResponse({"result": res})

