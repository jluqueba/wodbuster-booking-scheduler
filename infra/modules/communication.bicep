// communication.bicep
//
// Azure Communication Services (ACS) Email for outbound notification emails
// (ADR-0011). Provisions:
//   - an Email Communication Service + a CustomerManaged custom domain
//     (wodbuster-booking-scheduler.jluqueba.es) whose DNS verification records
//     are exposed as outputs for the operator to add at IONOS;
//   - a sender username (no-reply) with the "WodBuster Booking Scheduler"
//     display name;
//   - a Communication Services resource linked to the domain, which is the
//     endpoint the worker calls;
//   - a data-plane role assignment so the worker's user-assigned managed
//     identity can send (no connection string; DefaultAzureCredential).
//
// Two-phase, like the app custom domain: on the first provision the domain is
// created UNVERIFIED and the DNS records are emitted; the operator adds them at
// IONOS; once verified, sending works. Telegram is unaffected meanwhile.

@description('Short random token for globally-unique resource names.')
param resourceToken string

@description('Resource tags applied to every resource.')
param tags object

@description('Principal ID of the worker user-assigned managed identity, granted the ACS sender role.')
param appIdentityPrincipalId string

@description('Data residency for ACS/email content.')
param dataLocation string = 'Europe'

@description('Custom sending domain (a subdomain of jluqueba.es dedicated to this app).')
param senderDomain string = 'wodbuster-booking-scheduler.jluqueba.es'

@description('Local part of the from address.')
param senderUsername string = 'no-reply'

@description('From display name recipients see.')
param senderDisplayName string = 'WodBuster Booking Scheduler'

@description('Link the domain to the Communication Services resource. Must stay false until the domain is DNS-verified: ARM rejects linking an unverified CustomerManaged domain. Two-run flow like acaOutboundIps / the app custom-domain certificate.')
param linkVerifiedDomain bool = false

// Built-in role "Communication and Email Service Owner" (data-plane send).
// Verify in a new tenant with:
//   az role definition list --name "Communication and Email Service Owner" --query "[0].name" -o tsv
var communicationServiceOwnerRoleId = '09976791-48a7-449e-bb21-39d1a415f350'

resource emailService 'Microsoft.Communication/emailServices@2023-04-01' = {
  name: 'email-${resourceToken}'
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
  }
}

resource emailDomain 'Microsoft.Communication/emailServices/domains@2023-04-01' = {
  parent: emailService
  name: senderDomain
  location: 'global'
  tags: tags
  properties: {
    domainManagement: 'CustomerManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource sender 'Microsoft.Communication/emailServices/domains/senderUsernames@2023-04-01' = {
  parent: emailDomain
  name: senderUsername
  properties: {
    username: senderUsername
    displayName: senderDisplayName
  }
}

resource communicationService 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: 'acs-${resourceToken}'
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
    // Empty on phase 1 (domain not yet verified); populated on phase 2 once
    // the IONOS DNS records verify the domain (set AZURE_EMAIL_DOMAIN_LINKED=true).
    linkedDomains: linkVerifiedDomain ? [emailDomain.id] : []
  }
}

resource acsSenderAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: communicationService
  name: guid(communicationService.id, appIdentityPrincipalId, communicationServiceOwnerRoleId)
  properties: {
    principalId: appIdentityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      communicationServiceOwnerRoleId
    )
  }
}

@description('ACS endpoint the worker calls (https://<host>). Auth via managed identity.')
output acsEndpoint string = 'https://${communicationService.properties.hostName}'

@description('Full from address: <username>@<domain>.')
output senderAddress string = '${senderUsername}@${senderDomain}'

@description('From display name.')
output senderDisplayName string = senderDisplayName

@description('DNS records to add at IONOS to verify the domain and pass SPF/DKIM/DMARC.')
output dnsVerificationRecords object = emailDomain.properties.verificationRecords
