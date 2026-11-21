# Deploying to your own cluster

The manifests in `k8s/` work on any conformant Kubernetes cluster: a local one, GKE, EKS, AKS or self-hosted. The published images are public, so your nodes can pull them without registry credentials.

## Requirements

- Kubernetes **1.25 or newer**. The `devtoolkit` namespace uses Pod Security admission, which is built in from 1.25.
- A CNI that enforces NetworkPolicy, so that only `web` can reach `converter-api`. kindnet (the kind default), Calico, Cilium and GKE Dataplane V2 enforce it. Other CNIs accept the policy but ignore it.
- `kubectl` pointed at your cluster, with permission to create a namespace.
- Nodes that can pull from `ghcr.io`.

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

The manifests use the `:latest` images. For a deployment you rely on, pin exact builds. Every commit on `main` that changed a service has an image tagged with its full commit SHA.

Set the tag in the manifest before you apply it, so a later `kubectl apply` keeps the version:

```bash
sed -i 's#/converter-api:latest#/converter-api:<commit-sha>#' k8s/converter-api/deployment.yaml
sed -i 's#/web:latest#/web:<commit-sha>#' k8s/web/deployment.yaml
kubectl apply -f k8s/converter-api/ -f k8s/web/
```

`kubectl set image` also works, but the next `kubectl apply` of the unedited manifest switches back to `:latest` without warning.

## Try it

The Services are ClusterIP, so they are reachable only inside the cluster. To use the web UI from your machine, run:

```bash
kubectl -n devtoolkit port-forward svc/web 8080:8080
```

Then open http://localhost:8080. For public access, set up an Ingress or Gateway with TLS in your cluster and point it at the `web` Service only.

## Configure

See [Configuration](../configuration.md).

## Remove

```bash
kubectl delete namespace devtoolkit
```
