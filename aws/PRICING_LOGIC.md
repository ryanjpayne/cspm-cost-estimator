# AWS Cost Estimator - Pricing Logic

This document details how each AWS service cost is calculated in the cost estimator, including which services use live AWS Pricing API data vs. hardcoded estimates.

---

## Services Using AWS Pricing API

These services attempt to fetch live pricing data from the AWS Pricing API. If the API call fails, they fall back to hardcoded estimates.

### 1. AWS Lambda (`AWS::Lambda::Function`)

**Function:** `estimate_lambda_cost()`

**API Query:**
- Service Code: `AWSLambda`
- Filters:
  - Location: `{region}`
  - Group: `AWS-Lambda-Requests`

**Calculation:**
- Fetches price per 1M requests from API
- Fallback: $0.20 per 1M requests
- **Monthly Cost:** $0.00 (assumes free tier usage)
- **Notes:** Free tier includes 1M requests and 400K GB-seconds/month

**Accuracy:** ⚠️ **Partial** - Only fetches request pricing, doesn't calculate actual costs based on memory/duration. Shows $0.00 assuming free tier coverage.

---

### 2. AWS Secrets Manager (`AWS::SecretsManager::Secret`)

**Function:** `estimate_secrets_manager_cost()`

**API Query:**
- Service Code: `AWSSecretsManager`
- Filters:
  - Location: `{region}`
  - Product Family: `Secret`

**Calculation:**
- Fetches price per secret per month from API
- Fallback: $0.40 per secret/month
- **Monthly Cost:** Price from API (typically $0.40)
- **Notes:** Additional API call charges apply but are not calculated

**Accuracy:** ✅ **Good** - Accurately reflects per-secret monthly cost from API

---

### 3. NAT Gateway (`AWS::EC2::NatGateway`)

**Function:** `estimate_nat_gateway_cost()`

**API Query:**
- Service Code: `AmazonVPC`
- Filters:
  - Location: `{region}`
  - Product Family: `NAT Gateway`

**Calculation:**
- Fetches hourly price from API (fallback: $0.045/hour)
- Calculates monthly hourly cost: Hourly price × 730 hours (~$32.85/month)
- Adds data processing charges: $0.045/GB
- Estimates 20 GB/month data processing (~$0.90/month)
- **Monthly Cost:** Hourly cost + Data processing cost (~$33.75/month total)

**Accuracy:** ✅ **Excellent** - Now includes both hourly charges and data processing estimates

**Notes:**
- Hourly charge is consistent and predictable
- Data processing varies significantly by usage:
  - Light usage (5-20 GB/month): $0.23-$0.90/month additional
  - Medium usage (50-200 GB/month): $2.25-$9/month additional
  - Heavy usage (500+ GB/month): $22.50+/month additional

---

### 4. SQS Queue (`AWS::SQS::Queue`)

**Function:** `estimate_sqs_cost()`

**API Query:**
- Service Code: `AmazonSQS`
- Filters:
  - Location: `{region}`
  - Queue Type: `Standard` or `FIFO`

**Calculation:**
- Fetches price per million requests from API
- Fallback: $0.40 (Standard) or $0.50 (FIFO) per million requests
- Estimates 30,000 requests/month (CloudTrail S3 notifications)
- Applies 1M free tier
- **Monthly Cost:** $0.00 (within free tier for estimated usage)

**Accuracy:** ✅ **Good** - Accurately reflects SQS pricing with free tier consideration

---

### 5. CloudWatch Logs (`AWS::Logs::LogGroup`)

**Function:** `estimate_cloudwatch_logs_cost()`

**API Queries:**
1. **Log Ingestion:**
   - Service Code: `AmazonCloudWatch`
   - Filters:
     - Location: `{region}`
     - Product Family: `Data Ingestion`
     - Group Description: `Log Ingestion`

2. **Log Storage:**
   - Service Code: `AmazonCloudWatch`
   - Filters:
     - Location: `{region}`
     - Product Family: `Log Storage`
     - Group Description: `Log Storage`

**Calculation:**
- Fetches ingestion price per GB (fallback: $0.50/GB)
- Fetches storage price per GB/month (fallback: $0.03/GB)
- Estimates 0.02 GB ingestion/month (10K Lambda invocations × 2KB)
- Calculates storage based on retention period
- **Monthly Cost:** Ingestion cost + Storage cost (typically ~$0.01/month)

**Accuracy:** ✅ **Good** - Accurately reflects CloudWatch Logs pricing model with both ingestion and storage

---

## Services Using Hardcoded Estimates (No API Calls)

These services use fixed pricing values without querying the AWS Pricing API.

### 6. CloudTrail (`AWS::CloudTrail::Trail`)

**Function:** `estimate_cloudtrail_cost()`

**Pricing Model:** Hardcoded
- **Price:** $2.00 per 100,000 management events
- **Estimate:** 1,000,000 events/month
- **Monthly Cost:** $20.00

**Calculation:**
```
(1,000,000 events / 100,000) × $2.00 = $20.00
```

**Accuracy:** ✅ **Good** - CloudTrail pricing is straightforward and stable. First trail copy is free; this assumes an additional trail.

**Notes:**
- First copy of management events is FREE
- This pricing applies to additional trails
- Data events (S3/Lambda) would incur additional charges if enabled

---

### 7. EventBridge Rules (`AWS::Events::Rule`)

**Function:** `estimate_eventbridge_data_transfer_cost()`

**Pricing Model:** Hardcoded
- **Event Ingestion:** $0.00 (FREE for AWS management events)
- **Cross-Account Delivery:** $1.00 per million events
- **Estimate:** 5,000,000 events/month delivered cross-account
- **Monthly Cost:** $5.00 per rule

**Calculation:**
```
(5,000,000 events / 1,000,000) × $1.00 = $5.00
```

**Accuracy:** ✅ **Excellent** - Matches AWS's published pricing example exactly

**Notes:**
- Only charges for cross-account delivery
- Events must be ≤64KB (larger events count as multiple)
- Ingestion is always free for AWS management events

---

## Free Services (No Charges)

These services have no direct costs and are marked as free:

### 8. IAM Resources
- `AWS::IAM::Role`
- `AWS::IAM::Policy`
- `AWS::IAM::InstanceProfile`
- `AWS::IAM::ManagedPolicy`

**Cost:** $0.00 - IAM is a free service

---

### 9. VPC Networking (Most Components)
- `AWS::EC2::VPC`
- `AWS::EC2::Subnet`
- `AWS::EC2::InternetGateway`
- `AWS::EC2::RouteTable`
- `AWS::EC2::Route`
- `AWS::EC2::SecurityGroup`
- `AWS::EC2::NetworkAcl`
- `AWS::EC2::NetworkAclEntry`
- `AWS::EC2::VPCGatewayAttachment`
- `AWS::EC2::SubnetRouteTableAssociation`
- `AWS::EC2::SubnetNetworkAclAssociation`
- `AWS::EC2::VPCEndpoint` (Gateway endpoints for S3/DynamoDB)
- `AWS::EC2::EIP` (when attached)

**Cost:** $0.00 - These networking components are free

**Note:** NAT Gateways and Interface VPC Endpoints DO have costs

---

### 10. CloudFormation
- `AWS::CloudFormation::Stack`
- `AWS::CloudFormation::CustomResource`

**Cost:** $0.00 - CloudFormation is a free service

---

### 11. SSM Parameter Store (Standard Parameters)
- `AWS::SSM::Parameter`

**Cost:** $0.00 for standard parameters
- Up to 10,000 parameters per region
- 4KB maximum size
- 40 transactions per second (TPS)

**Note:** Advanced parameters incur charges

---

### 12. Database Subnet Groups
- `AWS::RDS::DBSubnetGroup`
- `AWS::Redshift::ClusterSubnetGroup`

**Cost:** $0.00 - Subnet groups themselves are free

---

### 13. KMS (Key Management) (`AWS::KMS::Key`)

**Function:** `estimate_kms_cost()`

**API Query:**
- Service Code: `awskms`
- Filters:
  - Location: `{region}`
  - Product Family: `Key Management`

**Calculation:**
- Fetches price per key per month from API
- Fallback: $1.00 per key/month
- **Monthly Cost:** $1.00 per customer managed key
- **Notes:** API request charges ($0.03 per 10,000 requests after 20,000 free tier) are mentioned but not calculated

**Accuracy:** ✅ **Good** - Accurately reflects per-key monthly cost from API

---

### 14. KMS Alias (`AWS::KMS::Alias`)

**Cost:** $0.00 - Aliases themselves are free

---

### 15. SNS Topic (`AWS::SNS::Topic`)

**Function:** `estimate_sns_cost()`

**API Query:**
- Service Code: `AmazonSNS`
- Filters:
  - Location: `{region}`
  - Product Family: `API Request`

**Calculation:**
- Fetches price per million requests from API
- Fallback: $0.50 per million requests
- Estimates 10,000 notifications/month (CloudTrail S3 objects)
- Applies 100K free tier for HTTP/HTTPS
- **Monthly Cost:** $0.00 (within free tier for estimated usage)
- **Notes:** SNS to SQS/Lambda delivery is FREE. Other protocols have varying costs.

**Accuracy:** ✅ **Good** - Accurately reflects SNS pricing with free tier consideration

---

### 16. SNS Subscription (`AWS::SNS::Subscription`)

**Cost:** $0.00 - Subscriptions themselves are free (delivery costs apply to the topic)

---

### 17. SQS Queue Policy
- `AWS::SQS::QueuePolicy`

**Cost:** $0.00 - Policies themselves are free (requests are charged)

---

## Summary of Pricing Accuracy

| Service | API Used | Notes |
|---------|----------|-------|
| Lambda | ✅ Yes | Only request pricing fetched; assumes free tier |
| Secrets Manager | ✅ Yes | Accurate per-secret cost |
| NAT Gateway | ✅ Yes | Includes hourly + data processing |
| SQS | ✅ Yes | Includes free tier calculation |
| CloudWatch Logs | ✅ Yes | Both ingestion and storage |
| KMS | ✅ Yes | Accurate per-key monthly cost |
| SNS | ✅ Yes | Includes free tier calculation |
| CloudTrail | ❌ No | Hardcoded but accurate |
| EventBridge | ❌ No | Hardcoded but matches AWS example |
| IAM | ❌ No | Free services |
| VPC Components | ❌ No | Free services |
| CloudFormation | ❌ No | Free services |
| SSM Parameters | ❌ No | Free for standard params |
