# Deploy the MediSlot dashboard to EC2 (CloudFormation only)

Region: us-east-1. Run these in AWS CloudShell or any terminal with the AWS CLI.

1. Push the changed files so GitHub Actions redeploys the network, IAM and
   application stacks (network-stack now lets the EC2 reach the VPC endpoints).

2. Upload the frontend zip (contains a top-level `frontend/` folder, no .venv):

       BUCKET=$(aws cloudformation describe-stacks --stack-name medislot-data-dev \
         --query "Stacks[0].Outputs[?OutputKey=='ApplicationFilesBucketName'].OutputValue" --output text)
       aws s3 cp medislot-frontend.zip s3://$BUCKET/frontend/medislot-frontend.zip

3. Create the EC2 stack:

       API=$(aws cloudformation describe-stacks --stack-name medislot-application-dev \
         --query "Stacks[0].Outputs[?OutputKey=='ApiBaseUrl'].OutputValue" --output text)
       aws cloudformation deploy --stack-name medislot-frontend-dev \
         --template-file cloudformation/frontend-stack.yaml \
         --capabilities CAPABILITY_IAM \
         --parameter-overrides Environment=dev ApiBaseUrl=$API FrontendCodeBucket=$BUCKET

4. Open the DashboardUrl output:

       aws cloudformation describe-stacks --stack-name medislot-frontend-dev \
         --query "Stacks[0].Outputs[?OutputKey=='DashboardUrl'].OutputValue" --output text

Update the dashboard later: upload a new zip to the same S3 key, then run the
RedeployCommand output (or `aws ssm send-command ... medislot-deploy`).
Logs on the server: /var/log/medislot-userdata.log, `journalctl -u medislot-web`.
