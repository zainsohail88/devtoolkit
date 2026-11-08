# Deploying to your own cluster

The manifests in `k8s/` work on any conformant Kubernetes cluster: a local one, GKE, EKS, AKS or self-hosted. Each published image is public, so no registry credentials are needed.

## Requirements

- Kubernetes **1.25 or newer**. The `devtoolkit` namespace uses Pod Security admission, which is built in from 1.25.
- A CNI that enforces NetworkPolicy, if you want `converter-api` to be reachable from `web` only. kind's default (kindnet), Calico, Cilium and GKE Dataplane V2 do. Without one the policy is accepted but has no effect.
- `kubectl` pointed at the cluster you want, with permission to create a namespace.
- Nodes able to pull from `ghcr.io`.

## Deploy

```bash
git clone https://github.com/zainsohail88/devtoolkit.git
cd devtoolkit
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/converter-api/ -f k8s/web/
kubectl -n devtoolkit rollout status deployment/converter-api deployment/web
```

This creates:

- the `devtoolkit` namespace
- the `converter-api` and `web` Deployments (2 replicas each)
- a ClusterIP Service for each: `converter-api` on port 8000, `web` on port 8080
- a ConfigMap for each
- a NetworkPolicy that lets only `web` pods reach `converter-api`

## Pin a version

The manifests use the `:latest` images. For anything you care about, pin exact builds: every commit on `main` that changed a service has an image tagged with its full commit SHA.

Pin it in the manifest itself, before applying, so a later `kubectl apply` keeps the version:

```bash
sed -i 's#/converter-api:latest#/converter-api:<commit-sha>#' k8s/converter-api/deployment.yaml
sed -i 's#/web:latest#/web:<commit-sha>#' k8s/web/deployment.yaml
kubectl apply -f k8s/converter-api/ -f k8s/web/
```

(`kubectl set image` also works, but the next `kubectl apply` of the unedited manifest silently switches back to `:latest`.)

## Try it

The Services are internal to the cluster (ClusterIP). To use the web UI from your machine:

```bash
kubectl -n devtoolkit port-forward svc/web 8080:8080
```

Then open http://localhost:8080. Public access through Ingress/Gateway with TLS is up to your cluster; point it at the `web` Service only.

## Configure

See [Configuration](../configuration.md).

## Remove

```bash
kubectl delete namespace devtoolkit
```
