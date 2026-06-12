# ---------------------------------------------------------------------------
# Remote state backend (S3 + DynamoDB lock).
#
# Backend blocks CANNOT use variables, so the values below must be filled in
# AFTER running the bootstrap stack. Two ways to do it:
#
#   A) Edit the placeholders below with the bootstrap outputs, then:
#        terraform init
#
#   B) Leave them and pass a partial config at init time:
#        terraform init \
#          -backend-config="bucket=family-media-tfstate-<ACCOUNT_ID>" \
#          -backend-config="dynamodb_table=family-media-tflock" \
#          -backend-config="region=us-east-1" \
#          -backend-config="key=family-media/terraform.tfstate"
#
# Until you have run bootstrap, you can comment out this whole block to use
# local state for a first dry run.
# ---------------------------------------------------------------------------

terraform {
  backend "s3" {
    bucket         = "family-media-tfstate-997599126378"
    key            = "family-media/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "family-media-tflock"
    encrypt        = true
  }
}
