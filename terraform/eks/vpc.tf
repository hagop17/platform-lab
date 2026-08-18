# vpc.tf
# A throwaway network, destroyed with the cluster. Public subnets and no
# NAT gateway: nodes get public IPs and the free internet gateway does the
# 1:1 translation. A NAT gateway would cost ~$0.045/hr and — more
# importantly — NAT gateways and their Elastic IPs are among the most
# common causes of a `terraform destroy` that hangs on "VPC has
# dependencies". Nothing is exposed inbound; access is kubectl
# port-forward through the EKS API server.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"

  # Both are required for the cluster's private endpoint to resolve inside
  # the VPC. enable_dns_hostnames defaults to FALSE on a non-default VPC;
  # without it, nodes silently cannot reach the control plane.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${var.cluster_name}-vpc"
  }
}

# Two subnets in two availability zones is mandatory: EKS refuses to
# create a cluster whose subnets span fewer than two, even with one node.
resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.${count.index + 1}.0/24"
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.cluster_name}-public-${count.index + 1}"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.cluster_name}-igw"
  }
}

# A subnet is "public" purely because its route table points at an
# internet gateway. There is no public/private flag in AWS.
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${var.cluster_name}-public"
  }
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}
