---
title: "Troubleshooting Kubernetes RBAC with kubectl auth can-i"
date: "2026-07-15T17:57:13+03:00"
lastmod: "2026-07-15T17:57:13+03:00"
description: "Learn how to diagnose Kubernetes RBAC authorization failures using kubectl auth can-i, with practical troubleshooting steps for verifying and resolving access issues."
summary: "This guide shows how to troubleshoot Kubernetes RBAC authorization failures by using kubectl auth can-i to test permissions and review roles and bindings."
tags: ["rbac", "kubernetes"]
categories: ["identity-access-management"]
publisher: "Compile My Mind"
draft: false
autonomous: true
last_reviewed: "2026-07-15"
verification_date: "2026-07-15T14:57:13.455802Z"
verification_version: "1"
version_context: "Documentation current at verification time"
recheck_after: "2026-09-13"
---

## Diagnosing Kubernetes Authorization Failures with kubectl auth can-i

When a Kubernetes user or service account unexpectedly loses access to resources, the root cause is often a misconfigured Role, ClusterRole, or binding. This guide is for cluster operators, application owners, and security engineers who need to diagnose and resolve RBAC-related authorization failures. The fastest way to verify permissions is with the `kubectl auth can-i` command, which checks whether a specific action is allowed for a user, group, or service account—without guessing or manually tracing policy YAML.

Direct answer: Use `kubectl auth can-i` to test whether a subject can perform a given action, and combine it with impersonation (when allowed) to troubleshoot permissions for other users or service accounts. This approach reveals exactly which RBAC rules are applied and helps pinpoint gaps or conflicts in your access policies.

## How RBAC Authorization Works in Kubernetes

Kubernetes RBAC (Role-Based Access Control) governs access to API resources based on defined roles and bindings. The API server evaluates every request against RBAC rules, and only allows actions explicitly permitted. Access is denied by default unless a policy grants it. RBAC objects include:

- **Role**: Grants permissions within a specific namespace.
- **ClusterRole**: Grants permissions cluster-wide or across all namespaces.
- **RoleBinding**: Assigns a Role to a user, group, or service account within a namespace.
- **ClusterRoleBinding**: Assigns a ClusterRole to a subject across the cluster.

This model enables fine-grained control but can be complex to debug, especially when multiple bindings overlap or when permissions are missing.

## Using kubectl auth can-i: Syntax and Scenarios

The `kubectl auth can-i` command checks if a subject is authorized to perform a specific action. Its syntax is flexible:

```bash
# Check if you can create pods in any namespace
kubectl auth can-i create pods --all-namespaces

# Check if you can list deployments in your current namespace
kubectl auth can-i list deployments.apps

# Check if a service account can list pods in another namespace (requires impersonation permission)
kubectl auth can-i list pods --as=system:serviceaccount:dev:foo -n prod

# Check if you can perform any action in your current namespace
kubectl auth can-i '*' '*'

# Check if you can get logs from pods
kubectl auth can-i get pods --subresource=log
```
Impersonation is useful for troubleshooting permissions for other users or service accounts. To use `--as`, the caller must be allowed to impersonate the target subject.

## Common RBAC Failure Modes and Diagnostic Workflow

Authorization failures typically manifest as HTTP 403 errors or denied actions in the Kubernetes API. Here’s a practical workflow for diagnosing RBAC issues:

1. **Identify the Subject and Action**: Determine which user, group, or service account is failing, and what action they attempted (e.g., creating a pod, listing secrets).
2. **Test with kubectl auth can-i**:
   - Run `kubectl auth can-i ${VERB} ${RESOURCE} -n ${NAMESPACE}` as the affected subject.
   - If impersonation is needed, use `--as` (with proper permission).
3. **Review RBAC Bindings**:
   - List Roles, ClusterRoles, RoleBindings, and ClusterRoleBindings relevant to the namespace or cluster.
   - Check for missing or misapplied bindings.
4. **Resolve and Retest**:
   - Adjust bindings or roles as needed.
   - Retest with `kubectl auth can-i` until the action is allowed.

### Example: Service Account Fails to Create Pods

Suppose a CI/CD pipeline uses the service account `system:serviceaccount:ci:builder` and fails to create pods in the `ci` namespace.

```bash
kubectl auth can-i create pods --as=system:serviceaccount:ci:builder -n ci
```

If the output is `no`, check RoleBindings in the `ci` namespace:

```bash
kubectl get rolebindings -n ci
```
Look for a binding that grants `create` on `pods` to the service account. If missing, create a Role and RoleBinding:

```bash
kubectl create role pod-creator --verb=create --resource=pods -n ci
kubectl create rolebinding builder-pod-creator --role=pod-creator --serviceaccount=ci:builder -n ci
```

Retest with `kubectl auth can-i`.

### Example: User Cannot List Secrets Cluster-Wide

A user attempts to list secrets across all namespaces but receives a 403 error. Test their permission:

```bash
kubectl auth can-i list secrets --all-namespaces --as=alice
```
If denied, check for a ClusterRoleBinding assigning the necessary ClusterRole to the user. If not present, update accordingly.

### Example: Application Fails to Access Custom Resource

An application using a service account cannot access a Custom Resource Definition (CRD) such as `jobs.batch`.

```bash
kubectl auth can-i get jobs.batch/bar --as=system:serviceaccount:app:runner -n app
```

If denied, verify that the Role or ClusterRole includes rules for the CRD and that the binding targets the correct service account.

## Reference Table: RBAC Object Scopes and Troubleshooting

| RBAC Object         | Scope           | Typical Use Case           | Troubleshooting Scenario                |
|---------------------|-----------------|----------------------------|-----------------------------------------|
| Role                | Namespace       | App-level permissions      | User can't access resource in namespace |
| ClusterRole         | Cluster-wide    | Admin, global permissions  | User can't access resource cluster-wide |
| RoleBinding         | Namespace       | Assign Role to subject     | Service account denied in namespace     |
| ClusterRoleBinding  | Cluster-wide    | Assign ClusterRole globally| User denied across multiple namespaces  |

## Security Considerations

RBAC objects are designed to restrict access. When troubleshooting, avoid granting overly broad permissions unless necessary. Always verify the minimum required privileges and test with `kubectl auth can-i` before deploying changes. Impersonation should be tightly controlled; only allow trusted users to use the `--as` option.

For more on Kubernetes security and access control, see [SIEM vs XDR vs SOAR](/posts/siem-vs-xdr-vs-soar/) for incident and access-monitoring context.

## Checklist for Troubleshooting RBAC with kubectl auth can-i

- [ ] Identify the subject (user, group, or service account) and action.
- [ ] Use `kubectl auth can-i` to test the action.
- [ ] If impersonation is needed, verify permission to use `--as`.
- [ ] Review relevant Roles, ClusterRoles, RoleBindings, and ClusterRoleBindings.
- [ ] Adjust RBAC objects as needed.
- [ ] Retest with `kubectl auth can-i`.
- [ ] Confirm least privilege and avoid unnecessary escalation.

## Conclusion

Troubleshooting Kubernetes RBAC is streamlined by using `kubectl auth can-i` to directly test permissions. This command, combined with careful review of RBAC objects and bindings, enables fast diagnosis and resolution of authorization failures. By following a structured workflow and maintaining strict security practices, you can ensure reliable access control in your Kubernetes clusters. For advanced workloads and custom resources, the same principles apply—test, review, and bind as needed. When in doubt, always verify with `kubectl auth can-i` before making changes.

If you operate AI/ML workloads on Kubernetes, see [Operating AI/ML Workloads on Kubernetes: A Headlamp Plugin for Kubeflow](/posts/operating-ai-ml-workloads-kubernetes/) for practical guidance on managing custom resources and RBAC in complex environments.

## Related guidance

- [The Heartbleed Vulnerability: A Deep Dive into the Buffer Over-Read Flaw](/posts/heartbleed-vulnerability-analysis/) — supporting reference.

## Sources

- [Using RBAC Authorization](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)
- [Authorization](https://kubernetes.io/docs/reference/access-authn-authz/authorization/)
- [kubectl auth can-i](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_auth/kubectl_auth_can-i/)
