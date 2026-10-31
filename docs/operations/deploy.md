# Deploying to your own cluster

The manifests in `k8s/` work on any conformant Kubernetes cluster: a local one, GKE, EKS, AKS or self-hosted. Each published image is public, so no registry credentials are needed.

## Requirements

- Kubernetes **1.25 or newer**. The `devtoolkit` namespace uses Pod Security admission, which is built in from 1.25.
- `kubectl` pointed at the cluster you want, with permission to create a namespace.
- Nodes able to pull from `ghcr.io`.

## Deploy

```bash
git clone https://github.com/zainsohail88/devtoolkit.git
cd devtoolkit
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/converter-api/
kubectl -n devtoolkit rollout status deployment/converter-api
```

This creates:

- the `devtoolkit` namespace
- the `converter-api` Deployment (2 replicas)
- a ClusterIP Service on port 8000
- a ConfigMap

## Pin a version

The manifest uses the `:latest` image. For anything you care about, pin an exact build: every commit on `main` that changed the service has an image tagged with its full commit SHA.

Pin it in the manifest itself, before applying, so a later `kubectl apply` keeps the version:

```bash
sed -i 's#/converter-api:latest#/converter-api:<commit-sha>#' k8s/converter-api/deployment.yaml
kubectl apply -f k8s/converter-api/
```

(`kubectl set image` also works, but the next `kubectl apply` of the unedited manifest silently switches back to `:latest`.)

## Try it

The Service is internal to the cluster (ClusterIP). To reach it from your machine:

```bash
kubectl -n devtoolkit port-forward svc/converter-api 8000:8000
curl localhost:8000/health
```

Public access through Ingress/Gateway with TLS is planned. See the [roadmap](../roadmap.md).

## Configure

See [Configuration](../configuration.md).

## Remove

```bash
kubectl delete namespace devtoolkit
```
